import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path as FilePath
from urllib.parse import quote, urlparse

import httpx
import redis
from fastapi import BackgroundTasks, HTTPException

from backends import BACKEND_HANDLERS
from config.loader import VERSION_CONFIGS, Version
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


def get_lock_name(file_hash: str, version: Version) -> str:
    """Return the Redis lock key for a given file hash and version."""
    return f"lock:processing:{file_hash}:{version.value}"


def build_s3_keys(file_hash: str, version: Version) -> tuple[str, str, str, str]:
    """Returns (s3_root_key, s3_content_key, s3_metadata_key, s3_imgs_key).

    s3_root_key     : documents/{hash}/{version_key}
    s3_content_key  : documents/{hash}/{version_key}/content.md
    s3_metadata_key : documents/{hash}/{version_key}/metadata.json   (backend metadata)
    s3_imgs_key     : documents/{hash}/{version_key}/images

    version_key is ProcessingConfig.cache_id if set, else version.value.
    This allows renaming a version without invalidating existing S3 cache.
    """
    version_key = VERSION_CONFIGS[version].cache_id or version.value
    root = f"documents/{file_hash}/{version_key}"
    return root, f"{root}/content.md", f"{root}/metadata.json", f"{root}/images"


def build_s3_bg_keys(
    file_hash: str, version: Version, mime: str
) -> tuple[str, str, str]:
    """Returns (source_key, alias_key, cache_meta_key) for background S3 updates.

    source_key      : documents/{hash}/source.{ext}   (ext derived from detected MIME type)
    alias_key       : documents/{hash}/_aliases.json
    cache_meta_key  : documents/{hash}/{version}/_metadata.json  (cache hit metadata)
    """
    ext = mime_to_ext(mime)
    version_key = VERSION_CONFIGS[version].cache_id or version.value
    source_key = f"documents/{file_hash}/source{ext}"
    alias_key = f"documents/{file_hash}/_aliases.json"
    cache_meta_key = f"documents/{file_hash}/{version_key}/_metadata.json"
    return source_key, alias_key, cache_meta_key


# ---------------------------------------------------------------------------
# S3 background update (source file, aliases, cache hit metadata)
# ---------------------------------------------------------------------------


async def background_update_s3(
    file_hash: str,
    version: Version,
    filename: str,
    content: bytes,
    mime: str,
) -> None:
    """Upload / update source file, _aliases.json and _metadata.json on S3.

    Processed document is out of scope (see update_s3_processed).
    This function includes a double lock:
     - Depending on the hashfile
     - Depending on the hashfile + version
    """
    if not settings.s3_cache_enabled:
        return

    source_key, alias_key, cache_meta_key = build_s3_bg_keys(file_hash, version, mime)
    now: datetime = datetime.now(tz=UTC)

    lock_name_unversioned: str = f"lock:uploading_source_file:{file_hash}"
    lock_name_versioned: str = get_lock_name(file_hash, version)

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
    s3_content_key: str,
    s3_metadata_key: str,
    s3_imgs_key: str,
) -> None:
    """Upload the processed document (Markdown, metadata, images) to S3.

    Must be called within the versioned Redis lock, not as a background task.
    """
    await s3_put_content(s3_content_key, processed_document.page_content)
    # metadata and images are optional
    if processed_document.metadata:
        await s3_put_pydantic(s3_metadata_key, processed_document.metadata)
    if processed_document.images:
        await s3_put_imgs(s3_imgs_key, processed_document.images)


# ---------------------------------------------------------------------------
# Failed request archiving
# ---------------------------------------------------------------------------


async def save_failed_request(
    file_content: bytes,
    filename: str,
    file_hash: str,
    version: Version,
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
    version: Version,
    file_hash: str,
    filename: str,
    file_content: bytes,
    upstream_headers: dict[str, str],
    s3_content_key: str,
    s3_metadata_key: str,
    s3_imgs_key: str,
    lock_name: str,
    force_reprocess: bool,
) -> tuple[ProcessedDocument, bool]:
    """Resolve a document from cache or by calling the upstream backend.

    Returns (ProcessedDocument, from_cache).
    On cache hit, ProcessedDocument.images is always empty — use s3_get_imgs() if images are needed.
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
                version=version,
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
                    processed_document, s3_content_key, s3_metadata_key, s3_imgs_key
                )
            except Exception as e:
                logger.warning(
                    f"CACHE [{version.value}] | WRITE ERROR - result not cached | File: {filename} | Error: {e}"
                )

        logger.info(
            f"PROC [{version.value}] | OK | Upstream: {upstream_duration:.0f} ms | File: {filename}"
        )
        return processed_document, False


# ---------------------------------------------------------------------------
# Upstream backend call
# ---------------------------------------------------------------------------


async def call_upstream_backend(
    version: Version, file_content: bytes, headers: dict[str, str], filename: str
) -> ProcessedDocument:
    """Send the file to the appropriate upstream backend and return a ProcessedDocument.

    Routing is backend_type-based (from versions.toml). Raises on unknown backend,
    non-2xx HTTP status, or empty page_content.
    """
    config: ProcessingConfig = VERSION_CONFIGS[version]
    handler = BACKEND_HANDLERS.get(config.backend_type)
    if handler is None:
        raise NotImplementedError(
            f"No backend handler registered for backend_type='{config.backend_type}'. "
            f"Available: {list(BACKEND_HANDLERS)}"
        )
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as async_client:
        return await handler(config, file_content, headers, filename, async_client)


# ---------------------------------------------------------------------------
# Shared request preamble (used by both process routes in main.py)
# ---------------------------------------------------------------------------


async def resolve_request(
    headers_data: ExternalDocumentRequestHeaders,
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: str,
    file_content: bytes,
    force_reprocess: bool,
    route: str = "",
) -> tuple[ProcessedDocument, bool, str, str, str, float]:
    """Shared preamble for both process routes.

    Computes the hash, logs the incoming request, schedules the S3 background
    update, and resolves the document (cache or upstream).

    Returns (processed_document, from_cache, filename, file_hash, s3_imgs_key, start_time).
    Raises HTTPException on lock timeout (504) or upstream failure (502).
    """
    start_time = time.perf_counter()

    file_hash, mime = await asyncio.gather(
        asyncio.to_thread(compute_hash, file_content),
        asyncio.to_thread(get_mime_type, file_content),
    )
    filename = headers_data.filename

    label = f" | {route}" if route else ""
    logger.info(
        f"REQ [{version.value}]{label} | File: {filename} | Hash: {file_hash} | MIME: {mime} | ClientKey: {api_key[:4]}***"
    )

    _, s3_content_key, s3_metadata_key, s3_imgs_key = build_s3_keys(file_hash, version)
    lock_name = get_lock_name(file_hash, version)

    if s3_is_active():
        background_tasks.add_task(
            background_update_s3,
            file_hash,
            version,
            filename,
            file_content,
            mime,
        )

    try:
        processed_document, from_cache = await _resolve_document(
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
            s3_imgs_key=s3_imgs_key,
            lock_name=lock_name,
            force_reprocess=force_reprocess,
        )
    except redis.exceptions.LockError:
        duration = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"LOCK [{version.value}]{label} | TIMEOUT | File: {filename} | Hash: {file_hash} | Duration: {duration:.0f} ms"
        )
        raise HTTPException(
            status_code=504,
            detail=f"[{version.value}] Lock timeout while processing '{filename}'. The file may already be queued — retry shortly.",
        )
    except Exception as e:
        duration = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"PROC [{version.value}]{label} | UPSTREAM FAIL | File: {filename} | Hash: {file_hash} | Duration: {duration:.0f} ms | Error: {e}"
        )
        raise HTTPException(
            status_code=502,
            detail=f"[{version.value}] Upstream processing failed for '{filename}': {str(e)}",
        )

    return processed_document, from_cache, filename, file_hash, s3_imgs_key, start_time


# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------


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
