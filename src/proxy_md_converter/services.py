import logging
from datetime import UTC, datetime
from pathlib import Path as FilePath
import hashlib
from typing import TYPE_CHECKING, Any
import time

from redis import Redis
import httpx
import boto3
from botocore.exceptions import ClientError

# enable type-checking without needing dev dependencies at runtime
if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
else:
    S3Client = Any

from .config import settings, Version
from .models import VersionMetadata, GlobalFileAliases

logger = logging.getLogger("proxy_md_converter")

s3_client: S3Client = boto3.client(
    service_name="s3",
    endpoint_url=settings.S3_ENDPOINT,
    aws_access_key_id=settings.S3_ACCESS_KEY,
    aws_secret_access_key=settings.S3_SECRET_KEY,
)

redis_client: Redis = Redis(
    host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=0, decode_responses=True
)


def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def get_extension(filename: str) -> str:
    return FilePath(filename).suffix.lower()


def s3_key_exists(key: str) -> bool:
    try:
        s3_client.head_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def background_update_task(
    file_hash: str,
    version: Version,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> None:
    """
    Upload / Update file source, aliases and metadata on S3
    """
    ext: str = get_extension(filename) or ".bin"
    source_key: str = f"documents/{file_hash}/source{ext}"
    alias_key: str = f"documents/{file_hash}/_aliases.json"
    meta_key: str = f"documents/{file_hash}/{version}/metadata.json"
    now: datetime = datetime.now(tz=UTC)
    try:
        # Upload source file to S3 if new hash
        if not s3_key_exists(source_key):
            start_time = time.perf_counter()
            # logger.info(f"BG [{version}] | Uploading source file: {filename} | Hash: {file_hash}")
            s3_client.put_object(
                Bucket=settings.S3_BUCKET,
                Key=source_key,
                Body=content,
                ContentType=content_type,
                Metadata={"original_name": filename},
            )
            duration = (time.perf_counter() - start_time) * 1000
            logger.info(
                f"BG [{version}] | Uploaded to S3 | Duration: {duration:.2f}ms | File: {filename} | Hash: {file_hash}"
            )

        # Create / Update Aliases file
        if s3_key_exists(alias_key):
            obj = s3_client.get_object(Bucket=settings.S3_BUCKET, Key=alias_key)
            raw_data: str = obj["Body"].read().decode("utf-8")
            aliases = GlobalFileAliases.model_validate_json(raw_data)
            if filename not in aliases.filenames:
                logger.debug(f"BG [{version}] | Adding alias | File: {filename}")
                aliases.filenames.append(filename)
                s3_client.put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=alias_key,
                    Body=aliases.model_dump_json(),
                )
        else:
            logger.debug(f"BG [{version}] | Creating new alias file | File: {filename}")
            new_aliases = GlobalFileAliases(file_hash=file_hash, filenames=[filename])
            s3_client.put_object(
                Bucket=settings.S3_BUCKET,
                Key=alias_key,
                Body=new_aliases.model_dump_json(),
            )

        # Create / Update Metada file
        if s3_key_exists(meta_key):
            obj_meta = s3_client.get_object(Bucket=settings.S3_BUCKET, Key=meta_key)
            raw_meta: str = obj_meta["Body"].read().decode("utf-8")
            meta = VersionMetadata.model_validate_json(raw_meta)
            meta.last_hit_at = now
            meta.hit_count += 1
            meta.last_filename_used = filename
        else:
            logger.debug(f"BG [{version}] | Creating metadata | Hash: {file_hash}")
            meta = VersionMetadata(
                version=version,
                created_at=now,
                last_hit_at=now,
                hit_count=1,
                last_filename_used=filename,
            )
        s3_client.put_object(
            Bucket=settings.S3_BUCKET, Key=meta_key, Body=meta.model_dump_json()
        )

    except Exception as e:
        logger.error(f"BG [{version}] | ERROR | Hash: {file_hash} | Error: {e}")


async def call_upstream_backend(
    url: str, content: bytes, headers: dict[str, str], params: dict[str, str | bool]
) -> dict:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.put(url, content=content, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
