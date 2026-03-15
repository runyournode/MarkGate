import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Annotated
from pathlib import Path

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

from config import Version, settings
from schemas import (
    ExternalDocumentRequestHeaders,
    ResponseDocument,
    ProxyOutput,
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
    build_tar_zst,
    pil_to_bytes,
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

    background_tasks.add_task(
        background_update_s3, file_hash, version, filename, file_content, headers_data.content_type
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
    responses={200: {"content": {"application/zstd": {}}, "description": "tar.zst archive"}},
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

    background_tasks.add_task(
        background_update_s3, file_hash, version, filename, file_content, headers_data.content_type
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
    if from_cache:
        images_bytes: dict[str, bytes] = await s3_get_imgs(s3_imgs_key)
    else:
        images_bytes = {
            name: pil_to_bytes(img)
            for name, img in processed_document.images.items()
        }

    archive = build_tar_zst(processed_document.page_content, images_bytes, processed_document.metadata)

    duration = (time.perf_counter() - start_time) * 1000
    logger.info(f"RES [{version.value}] | DOWNLOAD | Total: {duration:.0f} ms | File: {filename}")

    return Response(
        content=archive,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="{filename}.tar.zst"'},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)