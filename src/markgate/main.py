import logging
import time
from collections.abc import Awaitable
from logging.handlers import RotatingFileHandler
from typing import Annotated
from pathlib import Path
from urllib.parse import urlparse

import httpx
import redis
from fastapi_offline import FastAPIOffline
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi import (
    FastAPI,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)

from config import Version, VERSION_CONFIGS, settings
from schemas import (
    ExternalDocumentRequestHeaders,
    ResponseDocument,
    ProxyOutput,
    ServiceHealth,
    DependenciesHealth,
)
from services import (
    background_update_s3,
    compute_hash,
    get_lock_name,
    _resolve_document,
)
from utils import (
    lifespan,
    verify_api_key,
    s3_get_imgs,
    s3_is_active,
    check_s3_health,
    build_tar_zst,
    pil_to_bytes,
    redis_manager,
)

# --- Logging Configuration ---
logger = logging.getLogger("markgate")
logger.setLevel(settings.LOG_LEVEL)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File Handler (Optional)
if settings.LOG_FILE:
    file_handler = RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

app: FastAPI = FastAPIOffline(
    title="MarkGate",
    description="""
<div align="center">
    <img src="/statics/markgate_banner.jpg" alt="MarkGate Banner" width="500" />
    </br>
    <b>MarkGate</b>, a proxy for Markdown converter backends with persistent and versioned cache.
</div>
    """,
    favicon_url="/favicon.ico",
    lifespan=lifespan,
)

STATICS_DIR: Path = Path(__file__).resolve().parent / "statics"
app.mount("/statics", StaticFiles(directory=STATICS_DIR), name="statics")


# --- Global Exception Handler ---
@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        logger.error(f"HTTP {exc.status_code} | {exc.detail}")
    else:
        logger.warning(f"HTTP {exc.status_code} | {exc.detail}")

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


def _build_s3_keys(file_hash: str, version: Version) -> tuple[str, str, str, str]:
    """Returns (s3_root_key, s3_content_key, s3_metadata_key, s3_imgs_key)."""
    root = f"documents/{file_hash}/{version.value}"
    return root, f"{root}/content.md", f"{root}/metadata.json", f"{root}/images"


@app.put(
    "/md/{version}/process",
    response_model=ProxyOutput,
)
async def process_document(
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: Annotated[str, Depends(verify_api_key)],
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    force_reprocess: bool = Query(False),
) -> ProxyOutput | dict:
    start_time = time.perf_counter()

    file_hash = compute_hash(file_content)
    filename = headers_data.filename  # human-readable filename, cannot be passed as header, use headers_data.x_filename

    logger.info(
        f"REQ [{version.value}] | File: {filename} | Hash: {file_hash} | ClientKey: {api_key[:4]}***"
    )

    _, s3_content_key, s3_metadata_key, s3_imgs_key = _build_s3_keys(file_hash, version)
    lock_name = get_lock_name(file_hash, version)

    if s3_is_active():
        background_tasks.add_task(
            background_update_s3,
            file_hash,
            version,
            filename,
            file_content,
            headers_data.content_type,
        )

    try:
        processed_document, _ = await _resolve_document(
            version=version,
            file_hash=file_hash,
            filename=filename,
            file_content=file_content,
            upstream_headers={
                "Content-Type": headers_data.content_type,
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
        raise HTTPException(
            status_code=504,
            detail=f"RES [{version.value}] | LOCK TIMEOUT | File: {filename} | Duration: {duration:.0f} ms",
        )
    except Exception as e:
        duration = (time.perf_counter() - start_time) * 1000
        raise HTTPException(
            status_code=502,
            detail=f"RES [{version.value}] | UPSTREAM FAIL | File: {filename} | Duration: {duration:.0f} ms | Error: {str(e)}",
        )

    duration = (time.perf_counter() - start_time) * 1000
    logger.info(f"RES [{version.value}] | Total: {duration:.0f} ms | File: {filename}")

    return ResponseDocument(
        page_content=processed_document.page_content,
        metadata=processed_document.metadata,
    )


@app.put(
    "/md/{version}/process/download",
    response_class=Response,
    responses={
        200: {"content": {"application/zstd": {}}, "description": "tar.zst archive"}
    },
)
async def process_document_download(
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: Annotated[str, Depends(verify_api_key)],
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    force_reprocess: bool = Query(False),
) -> Response:
    start_time = time.perf_counter()

    file_hash = compute_hash(file_content)
    filename = headers_data.filename

    logger.info(
        f"REQ [{version.value}] | DOWNLOAD | File: {filename} | Hash: {file_hash} | ClientKey: {api_key[:4]}***"
    )

    _, s3_content_key, s3_metadata_key, s3_imgs_key = _build_s3_keys(file_hash, version)
    lock_name = get_lock_name(file_hash, version)

    if s3_is_active():
        background_tasks.add_task(
            background_update_s3,
            file_hash,
            version,
            filename,
            file_content,
            headers_data.content_type,
        )

    try:
        processed_document, from_cache = await _resolve_document(
            version=version,
            file_hash=file_hash,
            filename=filename,
            file_content=file_content,
            upstream_headers={
                "Content-Type": headers_data.content_type,
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
        raise HTTPException(
            status_code=504,
            detail=f"RES [{version.value}] | LOCK TIMEOUT | DOWNLOAD | File: {filename} | Duration: {duration:.0f} ms",
        )
    except Exception as e:
        duration = (time.perf_counter() - start_time) * 1000
        raise HTTPException(
            status_code=502,
            detail=f"RES [{version.value}] | UPSTREAM FAIL | DOWNLOAD | File: {filename} | Duration: {duration:.0f} ms | Error: {str(e)}",
        )

    # Gather images: from S3 on cache hit, from ProcessedDocument on fresh upstream call
    images_error: str | None = None
    if from_cache:
        try:
            images_bytes: dict[str, bytes] = await s3_get_imgs(s3_imgs_key)
        except Exception as e:
            logger.warning(
                f"RES [{version.value}] | DOWNLOAD | S3 image retrieval failed: {e}"
            )
            images_bytes = {}
            images_error = f"Image retrieval from S3 failed: {e}"
    else:
        images_bytes = {
            name: pil_to_bytes(img) for name, img in processed_document.images.items()
        }

    archive = build_tar_zst(
        processed_document.page_content,
        images_bytes,
        processed_document.metadata,
        images_error,
    )

    duration = (time.perf_counter() - start_time) * 1000
    logger.info(
        f"RES [{version.value}] | DOWNLOAD | Total: {duration:.0f} ms | File: {filename}"
    )

    return Response(
        content=archive,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="{filename}.tar.zst"'},
    )


@app.get("/health", tags=["Health"])
async def health():
    """Application liveness: returns 200 if the app is running."""
    return {"status": "ok"}


@app.get("/health/dependencies", response_model=DependenciesHealth, tags=["Health"])
async def health_dependencies():
    """Dependency health check: Redis, S3 cache, and upstream processing backends.

    HTTP status:
    - 200: all configured dependencies healthy (or S3 disabled)
    - 207: S3 enabled but unreachable (app still functional, cache bypassed)
    - 503: Redis unreachable (app non-functional)
    """
    # --- Redis ---
    try:
        ping = redis_manager.client.ping()
        assert isinstance(ping, Awaitable), (
            "redis.asyncio.Redis.ping() did not return an awaitable"
        )
        if not await ping:
            raise RuntimeError("Redis ping did not return PONG")
        redis_status = ServiceHealth(status="ok")
    except Exception as e:
        redis_status = ServiceHealth(status="unhealthy", message=str(e))

    # --- S3 ---
    s3_status_str, s3_msg = await check_s3_health()
    s3_status = ServiceHealth(status=s3_status_str, message=s3_msg)

    # --- Backends: deduplicate by base URL, then map result back per version ---
    base_url_to_versions: dict[str, list[str]] = {}
    for ver, cfg in VERSION_CONFIGS.items():
        parsed = urlparse(cfg.upstream_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        base_url_to_versions.setdefault(base, []).append(ver.value)

    base_url_results: dict[str, ServiceHealth] = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for base_url in base_url_to_versions:
            try:
                resp = await client.get(f"{base_url}/health")
                if resp.is_success:
                    base_url_results[base_url] = ServiceHealth(status="ok")
                else:
                    base_url_results[base_url] = ServiceHealth(
                        status="degraded", message=f"HTTP {resp.status_code}"
                    )
            except Exception as e:
                base_url_results[base_url] = ServiceHealth(
                    status="unhealthy", message=str(e)
                )

    backends: dict[str, ServiceHealth] = {}
    for ver, cfg in VERSION_CONFIGS.items():
        parsed = urlparse(cfg.upstream_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        backends[ver.value] = base_url_results[base]

    result = DependenciesHealth(redis=redis_status, s3=s3_status, backends=backends)

    if redis_status.status == "unhealthy":
        return JSONResponse(status_code=503, content=result.model_dump())
    if s3_status.status == "degraded":
        return JSONResponse(status_code=207, content=result.model_dump())
    return result


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
