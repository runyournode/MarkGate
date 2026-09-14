import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path as FilePath
from typing import NamedTuple
from urllib.parse import quote, urlparse

import httpx
import redis
from fastapi import BackgroundTasks, HTTPException

from backends import BACKEND_HANDLERS
from config.loader import VERSION_CONFIGS
from config.settings import settings
from contracts import ProcessingConfig
from schemas import Metadata
from media import get_mime_type, mime_to_ext
from schemas import (
    S3Metadata,
    S3FileAliases,
    ExternalDocumentRequestHeaders,
    ProcessedDocument,
    FailedRequestInfo,
)
from storage import (
    s3_manager,
    s3_is_active,
    s3_put_content,
    s3_get_content,
    s3_put_imgs,
    s3_get_imgs,
    s3_key_exists,
    s3_get_pydantic,
    s3_put_pydantic,
    redis_manager,
)

logger = logging.getLogger("markgate")


# ---------------------------------------------------------------------------
# Key builders — single source of truth for all S3 paths and Redis lock names
# ---------------------------------------------------------------------------


def compute_hash(content: bytes) -> str:
    """Return the SHA-256 hex digest of the given bytes."""
    return hashlib.sha256(content).hexdigest()


def get_lock_name(file_hash: str, signature: str) -> str:
    """Return the Redis lock key for a given file hash and cache signature."""
    return f"lock:processing:{file_hash}:{signature}"


class ImageKeys(NamedTuple):
    """Where a processing's images live in S3."""

    root: str
    """Prefix of the image objects; an image's key is f"{root}/{its Markdown path}"."""
    names_key: str
    """_images_names.txt: Markdown paths of this processing's images, one per line (internal)."""


def images_root(file_hash: str, signature: str, config: ProcessingConfig) -> str:
    """documents/{hash}/{backend_type} for content-named images (shared by every processing of
    the backend, see ProcessingConfig.content_addressed_images), else
    documents/{hash}/{signature}/images."""
    if config.content_addressed_images:
        return f"documents/{file_hash}/{config.backend_type}"
    return f"documents/{file_hash}/{signature}/images"


def build_s3_keys(
    file_hash: str, signature: str, config: ProcessingConfig
) -> tuple[str, str, str, ImageKeys]:
    """Returns (s3_root_key, s3_content_key, s3_metadata_key, image_keys).

    s3_root_key     : documents/{hash}/{signature}
    s3_content_key  : documents/{hash}/{signature}/content.md
    s3_metadata_key : documents/{hash}/{signature}/metadata.json   (backend metadata)
    image_keys      : see ImageKeys / images_root()

    `signature` comes from ProcessingConfig.cache_key() — see that method's docstring for what
    identifies "the same processing" across Versions and per-request overrides.
    """
    root = f"documents/{file_hash}/{signature}"
    image_keys = ImageKeys(
        root=images_root(file_hash, signature, config),
        names_key=f"{root}/_images_names.txt",
    )
    return root, f"{root}/content.md", f"{root}/metadata.json", image_keys


def build_s3_bg_keys(file_hash: str, signature: str, mime: str) -> tuple[str, str, str]:
    """Returns (source_key, alias_key, cache_meta_key) for background S3 updates.

    source_key      : documents/{hash}/source.{ext}   (ext derived from detected MIME type)
    alias_key       : documents/{hash}/_aliases.json
    cache_meta_key  : documents/{hash}/{signature}/_metadata.json  (cache hit metadata)
    """
    ext = mime_to_ext(mime)
    source_key = f"documents/{file_hash}/source{ext}"
    alias_key = f"documents/{file_hash}/_aliases.json"
    cache_meta_key = f"documents/{file_hash}/{signature}/_metadata.json"
    return source_key, alias_key, cache_meta_key


async def background_update_s3(
    file_hash: str,
    version: Enum,
    signature: str,
    filename: str,
    content: bytes,
    mime: str,
) -> None:
    """Upload / update source file, _aliases.json and _metadata.json on S3.

    Processed document is out of scope (see update_s3_processed).
    This function includes a double lock:
     - Depending on the hashfile
     - Depending on the hashfile + cache signature
    """
    if not settings.s3_cache_enabled:
        return

    source_key, alias_key, cache_meta_key = build_s3_bg_keys(file_hash, signature, mime)
    now: datetime = datetime.now(tz=UTC)

    lock_name_unversioned: str = f"lock:uploading_source_file:{file_hash}"
    lock_name_versioned: str = get_lock_name(file_hash, signature)

    async with (
        redis_manager.client.lock(
            lock_name_unversioned,
            timeout=settings.redis_lock_timeout,
            blocking_timeout=settings.redis_blocking_timeout,
        ),
        redis_manager.client.lock(
            lock_name_versioned,
            timeout=settings.redis_lock_timeout,
            blocking_timeout=settings.redis_blocking_timeout,
        ),
    ):
        step = "unknown"
        try:
            # Upload source file to S3 if new hash
            step = "source upload"
            if not await s3_key_exists(source_key):
                start_time = time.perf_counter()
                logger.info(
                    f"BG [{version.value}] | Uploading source file: {filename} | Hash: {file_hash}"
                )
                await s3_manager.client.put_object(
                    Bucket=settings.s3_bucket,
                    Key=source_key,
                    Body=content,
                    ContentType=mime,
                    Metadata={"original_name": quote(filename)},
                    ContentDisposition=f"attachment; filename*=UTF-8''{quote(filename)}",
                )
                duration = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"BG [{version.value}] | Uploaded to S3 | Duration: {duration:.2f}ms | File: {filename} | Hash: {file_hash}"
                )

            # Create / Update _aliases file
            step = "aliases update"
            if await s3_key_exists(alias_key):
                aliases = await s3_get_pydantic(alias_key, S3FileAliases)

                if filename not in aliases.filenames:
                    logger.debug(
                        f"BG [{version.value}] | Adding alias | File: {filename}"
                    )
                    aliases.filenames.append(filename)
                    await s3_put_pydantic(alias_key, aliases)
            else:
                logger.debug(
                    f"BG [{version.value}] | Creating new alias file | File: {filename}"
                )
                new_aliases = S3FileAliases(file_hash=file_hash, filenames=[filename])
                await s3_put_pydantic(alias_key, new_aliases)

            # Create / Update _metadata file
            step = "_metadata update"
            if await s3_key_exists(cache_meta_key):
                logger.debug(
                    f"BG [{version.value}] | Updating _metadata | Hash: {file_hash}"
                )
                meta = await s3_get_pydantic(cache_meta_key, S3Metadata)
                meta.last_hit_at = now
                meta.hit_count += 1
                meta.last_filename_used = filename
            else:
                logger.debug(
                    f"BG [{version.value}] | Creating _metadata | Hash: {file_hash}"
                )
                meta = S3Metadata(
                    version=version.value,
                    created_at=now,
                    last_hit_at=now,
                    hit_count=1,
                    last_filename_used=filename,
                )
            await s3_put_pydantic(cache_meta_key, meta)

        except Exception as e:
            logger.error(
                f"BG [{version.value}] | ERROR during {step} | Hash: {file_hash} | File: {filename} | Error: {e}"
            )


async def update_s3_processed(
    processed_document: ProcessedDocument,
    config: ProcessingConfig,
    s3_content_key: str,
    s3_metadata_key: str,
    image_keys: ImageKeys,
    version: Enum,
    filename: str,
) -> None:
    """Upload the processed document (images, image names, metadata, Markdown) to S3.

    content.md is written last: its presence is what makes a cache hit, so an interrupted write
    never yields a hit with missing images or metadata.
    Must be called within the versioned Redis lock, not as a background task.
    """
    images = processed_document.images
    if images:
        start = time.perf_counter()
        written = await s3_put_imgs(
            image_keys.root, images, skip_existing=config.content_addressed_images
        )
        logger.info(
            f"CACHE [{version.value}] | WRITE images ({written} new / {len(images)}) | {(time.perf_counter() - start) * 1000:.0f} ms | File: {filename}"
        )
    await s3_put_content(
        image_keys.names_key, "\n".join(images), content_type="text/plain"
    )

    if processed_document.metadata:
        start = time.perf_counter()
        await s3_put_pydantic(s3_metadata_key, processed_document.metadata)
        logger.debug(
            f"CACHE [{version.value}] | WRITE metadata.json | {(time.perf_counter() - start) * 1000:.0f} ms | File: {filename}"
        )

    start = time.perf_counter()
    await s3_put_content(s3_content_key, processed_document.page_content)
    logger.info(
        f"CACHE [{version.value}] | WRITE content.md | {(time.perf_counter() - start) * 1000:.0f} ms | File: {filename}"
    )


async def save_failed_request(
    file_content: bytes,
    filename: str,
    file_hash: str,
    version: Enum,
    error_message: str,
    upstream_duration_ms: float,
) -> None:
    """Fire-and-forget: save failed request artifacts for debugging.
    Tries S3 first (if enabled), then falls back to FAILED_REQUESTS_LOCAL_DIR.
    """
    now = datetime.now(tz=UTC)
    prefix = f"{settings.failed_requests_s3_prefix}/{now.strftime('%Y%m%dT%H%M%S')}_{file_hash[:12]}_{version.value}"
    mime = await asyncio.to_thread(get_mime_type, file_content)
    ext = mime_to_ext(mime)
    info = FailedRequestInfo(
        timestamp=now,
        version=version.value,
        filename=filename,
        file_hash=file_hash,
        error_message=error_message,
        upstream_duration_ms=upstream_duration_ms,
    )

    if s3_is_active():
        try:
            await s3_manager.client.put_object(
                Bucket=settings.s3_bucket,
                Key=f"{prefix}/source{ext}",
                Body=file_content,
                ContentType=mime,
            )
            await s3_put_pydantic(f"{prefix}/error.json", info)
            logger.info(
                f"FAIL [{version.value}] | Saved failed request to S3 | Hash: {file_hash}"
            )
            return
        except Exception as e:
            logger.warning(
                f"FAIL [{version.value}] | Could not save failed request to S3, trying local fallback | Error: {e}"
            )

    if settings.failed_requests_local_dir:
        try:
            local_dir = FilePath(settings.failed_requests_local_dir) / prefix
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / f"source{ext}").write_bytes(file_content)
            (local_dir / "error.json").write_text(info.model_dump_json())
            logger.info(
                f"FAIL [{version.value}] | Saved failed request locally | Path: {local_dir} | Hash: {file_hash}"
            )
        except Exception as e:
            logger.error(
                f"FAIL [{version.value}] | Could not save failed request locally | Error: {e}"
            )
    else:
        logger.warning(
            f"FAIL [{version.value}] | Failed request not archived: S3 unavailable and FAILED_REQUESTS_LOCAL_DIR not set | Hash: {file_hash}"
        )


async def gather_images(
    processed_document: ProcessedDocument,
    from_cache: bool,
    image_keys: ImageKeys,
    version: Enum,
    filename: str,
) -> dict[str, bytes]:
    """Return this document's images (raw bytes, keyed by Markdown path).

    On a fresh upstream call, `processed_document.images` already carries them — no IO. On a cache
    hit, `_resolve_document()` always returns `images={}` (cache reads skip S3 image retrieval
    unless needed) — this reads the processing's _images_names.txt and fetches exactly those
    images, so only images of this processing are served even from a shared images folder.
    """
    if not from_cache:
        return processed_document.images

    start = time.perf_counter()
    if not await s3_key_exists(image_keys.names_key):
        logger.warning(
            f"CACHE [{version.value}] | _images_names.txt missing, serving no image | File: {filename}"
        )
        return {}
    names = [
        name
        for name in (await s3_get_content(image_keys.names_key)).splitlines()
        if name
    ]
    images = await s3_get_imgs(image_keys.root, names)
    if missing := [name for name in names if name not in images]:
        logger.warning(
            f"CACHE [{version.value}] | {len(missing)} image(s) listed but not in S3: {missing} | File: {filename}"
        )
    logger.info(
        f"CACHE [{version.value}] | READ images ({len(images)}) | "
        f"{(time.perf_counter() - start) * 1000:.0f} ms | File: {filename}"
    )
    return images


async def get_images_by_name(
    file_hash: str, config: ProcessingConfig, names: list[str]
) -> tuple[dict[str, bytes], list[str]]:
    """Fetch content-named images of a document by Markdown path (POST /images).
    Returns (found images, missing names). `config` only needs to be of the images' backend."""
    images = await s3_get_imgs(images_root(file_hash, "", config), names)
    return images, [name for name in names if name not in images]


# ---------------------------------------------------------------------------
# Core document resolution (cache or upstream)
# ---------------------------------------------------------------------------


async def _keep_lock_alive(lock, interval: float) -> None:
    """Extend the lock TTL periodically to prevent expiration during long upstream calls.
    Runs until canceled. Stops silently if the lock is already lost.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await lock.extend(lock.timeout, replace_ttl=True)
        except Exception:
            return


async def _resolve_document(
    config: ProcessingConfig,
    version: Enum,
    file_hash: str,
    filename: str,
    file_content: bytes,
    upstream_headers: dict[str, str],
    s3_content_key: str,
    s3_metadata_key: str,
    image_keys: ImageKeys,
    lock_name: str,
    force_reprocess: bool,
) -> tuple[ProcessedDocument, bool]:
    """Resolve a document from cache or by calling the upstream backend.

    Returns (ProcessedDocument, from_cache).
    On cache hit, ProcessedDocument.images is always empty — use gather_images() if images are needed.
    On upstream failure, saves artifacts to failed_requests/ in S3 then re-raises.
    """
    lock = redis_manager.client.lock(
        lock_name,
        timeout=settings.redis_lock_timeout,
        blocking_timeout=settings.redis_blocking_timeout,
        raise_on_release_error=False,
    )
    async with lock:
        s3_ok = s3_is_active()

        if s3_ok and not force_reprocess:
            try:
                if await s3_key_exists(s3_content_key):
                    s3_start = time.perf_counter()
                    page_content = await s3_get_content(s3_content_key)
                    metadata = (
                        await s3_get_pydantic(s3_metadata_key, Metadata)
                        if await s3_key_exists(s3_metadata_key)
                        else None
                    )
                    s3_duration = (time.perf_counter() - s3_start) * 1000
                    logger.info(
                        f"CACHE [{version.value}] | HIT | S3 read: {s3_duration:.0f} ms | File: {filename}"
                    )
                    return ProcessedDocument(
                        page_content=page_content, metadata=metadata, images={}
                    ), True
            except Exception as e:
                logger.warning(
                    f"CACHE [{version.value}] | READ ERROR - bypassing cache | File: {filename} | Error: {e}"
                )
                s3_ok = False

        if force_reprocess:
            log_prefix = "FORCED REPROCESS"
        elif not settings.s3_cache_enabled:
            log_prefix = "CACHE DISABLED"
        elif not s3_ok:
            log_prefix = "CACHE NOT REACHEABLE"
        else:
            log_prefix = "CACHE MISS"
        logger.info(
            f"PROC [{version.value}] | {log_prefix} | Calling upstream backend | File: {filename}"
        )

        upstream_start = time.perf_counter()
        extend_interval = max(10.0, settings.redis_lock_timeout / 2)
        extender = asyncio.create_task(_keep_lock_alive(lock, extend_interval))
        try:
            processed_document = await call_upstream_backend(
                config=config,
                file_content=file_content,
                headers=upstream_headers,
                filename=filename,
            )
        except Exception as e:
            upstream_duration_ms = (time.perf_counter() - upstream_start) * 1000
            asyncio.create_task(
                save_failed_request(
                    file_content=file_content,
                    filename=filename,
                    file_hash=file_hash,
                    version=version,
                    error_message=str(e),
                    upstream_duration_ms=upstream_duration_ms,
                )
            )
            raise
        finally:
            extender.cancel()
            try:
                await extender
            except asyncio.CancelledError:
                pass

        upstream_duration = (time.perf_counter() - upstream_start) * 1000

        if not s3_ok:
            logger.warning(
                f"CACHE [{version.value}] | WRITE SKIPPED - S3 unreachable (read failed earlier) - result not cached | File: {filename}"
            )
        else:
            try:
                await update_s3_processed(
                    processed_document,
                    config,
                    s3_content_key,
                    s3_metadata_key,
                    image_keys,
                    version,
                    filename,
                )
            except Exception as e:
                logger.warning(
                    f"CACHE [{version.value}] | WRITE ERROR - result not cached | File: {filename} | Error: {e}"
                )

        logger.info(
            f"PROC [{version.value}] | OK | Upstream: {upstream_duration:.0f} ms | File: {filename}"
        )
        return processed_document, False


async def call_upstream_backend(
    config: ProcessingConfig,
    file_content: bytes,
    headers: dict[str, str],
    filename: str,
) -> ProcessedDocument:
    """Send the file to the appropriate upstream backend and return a ProcessedDocument.

    `config` must already reflect any per-request overrides (see ProcessingConfig.with_overrides).
    Routing is backend_type-based. Raises on unknown backend, non-2xx HTTP status, or empty
    page_content. Stamps `extraction_engine` (backend_type, e.g. "foil"/"xberg") onto the result's
    metadata here, once, rather than in every backend module.
    """
    handler = BACKEND_HANDLERS.get(config.backend_type)
    if handler is None:
        raise NotImplementedError(
            f"No backend handler registered for backend_type='{config.backend_type}'. "
            f"Available: {list(BACKEND_HANDLERS)}"
        )
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as async_client:
        result = await handler(config, file_content, headers, filename, async_client)
    existing = result.metadata.root if result.metadata else {}
    result.metadata = Metadata({**existing, "extraction_engine": config.backend_type})
    return result


async def resolve_request(
    headers_data: ExternalDocumentRequestHeaders,
    version: Enum,
    config: ProcessingConfig,
    background_tasks: BackgroundTasks,
    api_key: str,
    file_content: bytes,
    force_reprocess: bool,
    route: str = "",
) -> tuple[ProcessedDocument, bool, str, str, ImageKeys, float]:
    """Shared preamble for all process routes.

    `config` is the Version's ProcessingConfig with any per-request overrides already applied
    (see ProcessingConfig.with_overrides) — callers look it up once and pass it down here.

    Computes the hash, logs the incoming request, schedules the S3 background
    update, and resolves the document (cache or upstream).

    The response metadata is completed with `source_sha256` and `extraction_engine` (not stored:
    both are known from the request), which a client needs to call POST /images.

    Returns (processed_document, from_cache, filename, file_hash, image_keys, start_time).
    Raises HTTPException on lock timeout (504) or upstream failure (502).
    """
    start_time = time.perf_counter()

    file_hash, mime = await asyncio.gather(
        asyncio.to_thread(compute_hash, file_content),
        asyncio.to_thread(get_mime_type, file_content),
    )
    filename = headers_data.filename
    signature = config.cache_key(version.value, mime)

    label = f" | {route}" if route else ""
    logger.info(
        f"REQ [{version.value}]{label} | File: {filename} | Hash: {file_hash} | MIME: {mime} | Key: {signature} | ClientKey: {api_key[:4]}***"
    )

    _, s3_content_key, s3_metadata_key, image_keys = build_s3_keys(
        file_hash, signature, config
    )
    lock_name = get_lock_name(file_hash, signature)

    if s3_is_active():
        background_tasks.add_task(
            background_update_s3,
            file_hash,
            version,
            signature,
            filename,
            file_content,
            mime,
        )

    try:
        processed_document, from_cache = await _resolve_document(
            config=config,
            version=version,
            file_hash=file_hash,
            filename=filename,
            file_content=file_content,
            upstream_headers={
                "Content-Type": mime,
                "X-Filename": headers_data.x_filename,
            },
            s3_content_key=s3_content_key,
            s3_metadata_key=s3_metadata_key,
            image_keys=image_keys,
            lock_name=lock_name,
            force_reprocess=force_reprocess,
        )
    except redis.exceptions.LockError as e:
        duration = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"LOCK [{version.value}]{label} | TIMEOUT | File: {filename} | Hash: {file_hash} | Duration: {duration:.0f} ms"
        )
        detail = f"[{version.value}] Lock timeout while processing '{filename}'. The file may already be queued — retry shortly."
        if settings.verbose_errors and str(e):
            detail += f"\nRedis error: {e}"
        raise HTTPException(status_code=504, detail=detail)
    except Exception as e:
        duration = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"PROC [{version.value}]{label} | UPSTREAM FAIL | File: {filename} | Hash: {file_hash} | Duration: {duration:.0f} ms | Error: {e}"
        )
        detail = (
            f"[{version.value}] Upstream processing failed for '{filename}': {str(e)}"
        )
        if settings.verbose_errors and isinstance(e, httpx.HTTPStatusError):
            detail += f"\nUpstream response body: {e.response.text}"
        raise HTTPException(status_code=502, detail=detail)

    existing = processed_document.metadata.root if processed_document.metadata else {}
    processed_document.metadata = Metadata(
        {
            **existing,
            "source_sha256": file_hash,
            "extraction_engine": config.backend_type,
        }
    )

    return processed_document, from_cache, filename, file_hash, image_keys, start_time


async def check_backends_health() -> dict[str, tuple[str, str | None]]:
    """Probe all upstream backends. Deduplicates by base URL.
    Returns {version_value: (status, message|None)}.
    status: 'ok' | 'degraded' | 'unhealthy'
    """
    base_url_to_versions: dict[str, list[str]] = {}
    for ver, cfg in VERSION_CONFIGS.items():
        parsed = urlparse(cfg.upstream_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        base_url_to_versions.setdefault(base, []).append(ver.value)

    base_results: dict[str, tuple[str, str | None]] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for base_url in base_url_to_versions:
            try:
                resp = await client.get(f"{base_url}/health")
                if resp.is_success:
                    base_results[base_url] = ("ok", None)
                else:
                    base_results[base_url] = ("degraded", f"HTTP {resp.status_code}")
            except Exception as e:
                base_results[base_url] = ("unhealthy", str(e))

    result: dict[str, tuple[str, str | None]] = {}
    for ver, cfg in VERSION_CONFIGS.items():
        parsed = urlparse(cfg.upstream_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        result[ver.value] = base_results[base]
    return result
