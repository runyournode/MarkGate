import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Annotated
from pathlib import Path

import redis
from fastapi_offline import FastAPIOffline
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from fastapi import (
    FastAPI,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Request,
)

from .config import VERSION_CONFIGS, ProcessingConfig, Version, settings
from .models import (
    ExternalDocumentRequestHeaders,
    ProcessedDocument,
    ProxyOutput,
    Metadata,
)
from .services import (
    background_update_s3,
    update_s3_processed,
    call_upstream_backend,
    compute_hash,
)
from .utils import (
    lifespan,
    redis_manager,
    verify_api_key,
    s3_key_exists,
    s3_get_content,
    s3_get_pydantic,
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
    <b>MarkGater</b>, a proxy for markdown converter backends with persistent and versioned cache.
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
    # Log the exception details
    if exc.status_code >= 500:
        logger.error(f"HTTP {exc.status_code} | {exc.detail}")
    else:
        logger.warning(f"HTTP {exc.status_code} | {exc.detail}")

    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


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
) -> ProxyOutput | dict:
    start_time = time.perf_counter()
    config: ProcessingConfig = VERSION_CONFIGS[version]

    file_hash: str = compute_hash(file_content)
    filename = headers_data.filename

    # Log incoming request
    logger.info(
        f"REQ [{version.value}] | File: {filename} | Hash: {file_hash} | ClientKey: {api_key[:4]}***"
    )

    s3_content_key: str = f"documents/{file_hash}/{version.value}/content.md"
    s3_metadata_key: str = f"documents/{file_hash}/{version.value}/metadata.json"
    lock_name: str = f"lock:{file_hash}:{version.value}"

    # Add/Update source file, _metadata.json and _aliases.json on S3
    # No need for a lock here as the function already manages locking
    background_tasks.add_task(
        background_update_s3,
        file_hash,
        version,
        filename,
        file_content,
        headers_data.content_type,
    )

    async with redis_manager.client.lock(lock_name, timeout=600, blocking_timeout=20):
        try:
            # Cache hit
            if await s3_key_exists(s3_content_key) and await s3_key_exists(
                s3_metadata_key
            ):
                s3_start = time.perf_counter()

                page_content = await s3_get_content(s3_content_key)
                metadata = await s3_get_pydantic(s3_metadata_key, Metadata)

                s3_duration = (time.perf_counter() - s3_start) * 1000
                duration = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"RES [{version.value}] | CACHE HIT | Duration: {duration:.0f} ms | S3 Read: {s3_duration:.0f} ms | File: {filename}"
                )
                return ProcessedDocument(page_content=page_content, metadata=metadata)
            # Cache miss
            else:
                logger.info(
                    f"PRC [{version.value}] | CACHE MISS | PROCESSING UPSTREAM | File: {filename}"
                )
                upstream_start = time.perf_counter()

                # Form request
                upstream_headers: dict[str, str] = {
                    "Content-Type": headers_data.content_type,
                    "X-Filename": headers_data.x_filename,
                }
                if config.custom_headers:  # api key(s) for the backend
                    upstream_headers.update(config.custom_headers)

                # Send request
                try:
                    processed_document: ProcessedDocument = await call_upstream_backend(
                        url=config.upstream_url,
                        content=file_content,
                        headers=upstream_headers,
                        params=config.query_params,
                    )
                    upstream_duration = (time.perf_counter() - upstream_start) * 1000

                    """
                    Write to S3 the processed document:
                    We don't use a background task as it should be quick to write and 
                      we must ensure files are written before lock is released. 
                    (We can't control who will win the next lock between this 
                      back-grounded task and another process_document, it could
                      result in requesting again the backend processor)
                    """
                    await update_s3_processed(
                        processed_document,
                        s3_content_key,
                        s3_metadata_key,
                    )

                    duration = (time.perf_counter() - start_time) * 1000

                    logger.info(
                        f"RES [{version.value}] | UPSTREAM OK | Total: {duration:.0f} ms | Upstream: {upstream_duration:.0f} ms | File: {filename}"
                    )
                    return processed_document
                except Exception as e:
                    upstream_duration = (time.perf_counter() - upstream_start) * 1000
                    raise HTTPException(
                        status_code=502,
                        detail=f"RES [{version.value}] | UPSTREAM FAIL | Upstream: {upstream_duration:.0f}ms | File: {filename} | Error: {str(e)}",
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
                status_code=500,
                detail=f"RES [{version.value}] | SYSTEM ERROR | File: {filename} | Duration: {duration:.0f} ms | Error: {str(e)}",
            )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")
