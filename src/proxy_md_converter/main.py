import json
import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Annotated

import redis
from botocore.exceptions import ClientError
from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import VERSION_CONFIGS, ProcessingConfig, Version, settings
from .models import ExternalDocumentOutput, ExternalDocumentRequestHeaders
from .services import (
    background_update_task,
    call_upstream_backend,
    compute_hash,
    redis_client,
    s3_client,
)

# --- Logging Configuration ---
logger = logging.getLogger("proxy_md_converter")
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


app = FastAPI(title="Proxy MD Converter")


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


# Use HTTPBearer for Authorization header
security = HTTPBearer(auto_error=False)


async def verify_api_key(
    version: Version,
    auth: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """
    Check if processing version is supported and if client has a proper API key.
    """
    api_key = auth.credentials if auth else None
    expected_key = VERSION_CONFIGS[version].authorized_api_key
    if not api_key or api_key != expected_key:
        masked_key = (api_key[:4] + "***") if api_key else "None"
        raise HTTPException(
            status_code=403,
            detail=f"Unauthorized access attempt for version {version}. Key provided: {masked_key}",
        )
    return api_key


@app.put(
    "/md/{version}/process",
    response_model=ExternalDocumentOutput,
)
async def process_document(
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_api_key),
    file_content: bytes = Body(...),
) -> ExternalDocumentOutput | dict:
    start_time = time.perf_counter()
    config: ProcessingConfig = VERSION_CONFIGS[version]

    file_hash: str = compute_hash(file_content)
    filename = headers_data.filename

    # Log incoming request
    logger.info(
        f"REQ [{version}] | File: {filename} | Hash: {file_hash} | ClientKey: {api_key[:4]}***"
    )

    s3_result_key: str = f"documents/{file_hash}/{version}/result.json"
    lock_name: str = f"lock:{file_hash}:{version}"

    background_tasks.add_task(
        background_update_task,
        file_hash,
        version,
        filename,
        file_content,
        headers_data.content_type,
    )

    try:
        with redis_client.lock(lock_name, timeout=600, blocking_timeout=20):
            # 1. Try Cache
            try:
                s3_start = time.perf_counter()
                obj = s3_client.get_object(Bucket=settings.S3_BUCKET, Key=s3_result_key)
                raw_json: str = obj["Body"].read().decode("utf-8")
                s3_duration = (time.perf_counter() - s3_start) * 1000

                duration = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"RES [{version}] | CACHE HIT | Duration: {duration:.2f}ms | S3 Read: {s3_duration:.2f}ms | File: {filename}"
                )

                return json.loads(raw_json)
            except ClientError:
                pass

            # 2. Process Upstream
            logger.info(f"PRC [{version}] | PROCESSING UPSTREAM | File: {filename}")
            upstream_start = time.perf_counter()

            upstream_headers: dict[str, str] = {
                "Content-Type": headers_data.content_type,
                "X-Filename": headers_data.x_filename,
            }
            if config.custom_headers:  # api key(s) for the backend
                upstream_headers.update(config.custom_headers)

            try:
                result_data: dict = await call_upstream_backend(
                    url=config.upstream_url,
                    content=file_content,
                    headers=upstream_headers,
                    params=config.query_params,
                )

                upstream_duration = (time.perf_counter() - upstream_start) * 1000

                # Write to S3
                s3_write_start = time.perf_counter()
                s3_client.put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=s3_result_key,
                    Body=json.dumps(result_data),
                    ContentType="application/json",
                )
                s3_write_duration = (time.perf_counter() - s3_write_start) * 1000

                duration = (time.perf_counter() - start_time) * 1000

                logger.info(
                    f"RES [{version}] | UPSTREAM OK | Total: {duration:.2f}ms | Upstream: {upstream_duration:.2f}ms | S3 Write: {s3_write_duration:.2f}ms | File: {filename}"
                )
                return result_data
            except Exception as e:
                upstream_duration = (time.perf_counter() - upstream_start) * 1000
                logger.error(
                    f"RES [{version}] | UPSTREAM FAIL | Upstream: {upstream_duration:.2f}ms | Error: {str(e)}"
                )
                raise e

    except redis.exceptions.LockError:
        duration = (time.perf_counter() - start_time) * 1000
        raise HTTPException(
            status_code=504,
            detail=f"RES [{version}] | LOCK TIMEOUT | File: {filename} | Duration: {duration:.2f}ms",
        )
    except Exception as e:
        duration = (time.perf_counter() - start_time) * 1000
        logger.error(
            f"RES [{version}] | SYSTEM ERROR | File: {filename} | Duration: {duration:.2f}ms | Error: {str(e)}"
        )
        raise HTTPException(status_code=502, detail=str(e))
