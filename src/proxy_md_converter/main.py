import json
import redis
from typing import Annotated
from fastapi import FastAPI, Header, HTTPException, Path, Request, BackgroundTasks
from botocore.exceptions import ClientError

from .config import VERSION_CONFIGS, S3_BUCKET, UPSTREAM_URL, ProcessingConfig
from .models import ExternalDocumentRequestHeaders, ExternalDocumentOutput
from .services import (
    s3_client,
    redis_client,
    compute_hash,
    background_update_task,
    call_upstream_backend,
)

app = FastAPI(title="Proxy MD Converter")


@app.put("/md/{version}/process", response_model=ExternalDocumentOutput)
async def process_document(
    request: Request,
    background_tasks: BackgroundTasks,
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: str = Path(...),
) -> ExternalDocumentOutput | dict:
    if version not in VERSION_CONFIGS:
        raise HTTPException(404, f"Version {version} not supported")
    config: ProcessingConfig = VERSION_CONFIGS[version]

    file_content: bytes = await request.body()
    if not file_content:
        raise HTTPException(400, "Empty body")

    file_hash: str = compute_hash(file_content)
    s3_result_key: str = f"documents/{file_hash}/{version}/result.json"
    lock_name: str = f"lock:{file_hash}:{version}"

    background_tasks.add_task(
        background_update_task,
        file_hash,
        version,
        headers_data.x_filename,
        file_content,
        headers_data.content_type,
    )

    try:
        with redis_client.lock(lock_name, timeout=600, blocking_timeout=20):
            try:
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_result_key)
                print(f"✅ CACHE HIT ({version})")
                raw_json: str = obj["Body"].read().decode("utf-8")
                return json.loads(raw_json)
            except ClientError:
                pass

            print(f"⚡ PROCESSING UPSTREAM ({version})...")

            upstream_headers: dict[str, str] = {
                "Content-Type": headers_data.content_type,
                "X-Filename": headers_data.x_filename,
            }
            if config["custom_headers"]:
                upstream_headers.update(config["custom_headers"])
            if headers_data.authorization:
                upstream_headers["Authorization"] = headers_data.authorization

            result_data: dict = await call_upstream_backend(
                f"{UPSTREAM_URL}/process",
                content=file_content,
                headers=upstream_headers,
                params=config["query_params"],
            )

            s3_client.put_object(
                Bucket=S3_BUCKET,
                Key=s3_result_key,
                Body=json.dumps(result_data),
                ContentType="application/json",
            )
            return result_data

    except redis.exceptions.LockError:
        raise HTTPException(status_code=504, detail="Resource busy, lock timeout.")
    except Exception as e:
        print(f"❌ Error: {e}")
        raise HTTPException(status_code=502, detail=str(e))
