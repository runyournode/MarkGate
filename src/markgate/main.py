import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    Header,
    Query,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi_offline import FastAPIOffline

from config.settings import settings
from media import build_tar_zst, batch_pil_to_bytes
from schemas import (
    ExternalDocumentRequestHeaders,
    ResponseDocument,
    ProxyOutput,
    ServiceHealth,
    DependenciesHealth,
)
from security import verify_api_key
from services import check_backends_health, resolve_request
from storage import check_redis_health, check_s3_health, lifespan, s3_get_imgs
from config.loader import Version

# --- Logging Configuration ---
logger = logging.getLogger("markgate")
logger.setLevel(settings.log_level)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

if settings.log_file:
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
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


# ---------------------------------------------------------------------------
# Process routes
# ---------------------------------------------------------------------------


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
    """Convert a document to Markdown. Returns page_content and metadata."""
    processed_document, _, filename, _, _, start_time = await resolve_request(
        headers_data, version, background_tasks, api_key, file_content, force_reprocess
    )

    duration = (time.perf_counter() - start_time) * 1000
    logger.info(f"RESP [{version.value}] | Total: {duration:.0f} ms | File: {filename}")

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
    """Convert a document to Markdown and return a tar.zst archive (content.md, images and metadata)."""
    (
        processed_document,
        from_cache,
        filename,
        _,
        s3_imgs_key,
        start_time,
    ) = await resolve_request(
        headers_data,
        version,
        background_tasks,
        api_key,
        file_content,
        force_reprocess,
        route="DOWNLOAD",
    )

    # Gather images: from S3 on cache hit, from ProcessedDocument on fresh upstream call
    images_error: str | None = None
    if from_cache:
        try:
            images_bytes: dict[str, bytes] = await s3_get_imgs(s3_imgs_key)
        except Exception as e:
            logger.warning(
                f"CACHE [{version.value}] | DOWNLOAD | S3 image retrieval failed | File: {filename} | Error: {e}"
            )
            images_bytes = {}
            images_error = f"Image retrieval from S3 failed: {e}"
    else:
        images_bytes = await asyncio.to_thread(
            batch_pil_to_bytes, processed_document.images
        )

    archive = await asyncio.to_thread(
        build_tar_zst,
        processed_document.page_content,
        images_bytes,
        processed_document.metadata,
        images_error,
    )

    duration = (time.perf_counter() - start_time) * 1000
    logger.info(
        f"RESP [{version.value}] | DOWNLOAD | Total: {duration:.0f} ms | File: {filename}"
    )

    return Response(
        content=archive,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="{filename}.tar.zst"'},
    )


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


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
    redis_status_str, redis_msg = await check_redis_health()
    redis_status = ServiceHealth(status=redis_status_str, message=redis_msg)

    # --- S3 ---
    s3_status_str, s3_msg = await check_s3_health()
    s3_status = ServiceHealth(status=s3_status_str, message=s3_msg)

    # --- Backends ---
    backends_raw = await check_backends_health()
    backends = {
        ver: ServiceHealth(status=status, message=msg)
        for ver, (status, msg) in backends_raw.items()
    }

    result = DependenciesHealth(redis=redis_status, s3=s3_status, backends=backends)

    if redis_status.status == "unhealthy":
        return JSONResponse(status_code=503, content=result.model_dump())
    if s3_status.status == "degraded":
        return JSONResponse(status_code=207, content=result.model_dump())
    return result


# ---------------------------------------------------------------------------
# Static / misc
# ---------------------------------------------------------------------------


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the favicon."""
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
