import logging
from datetime import UTC, datetime
from pathlib import Path as FilePath
import hashlib
import time

import httpx

from .config import settings, Version
from .models import S3Metadata, S3FileAliases, ProcessedDocument
from .utils import (
    s3_manager,
    s3_put_content,
    s3_key_exists,
    s3_get_pydantic,
    s3_put_pydantic,
    redis_manager,
)

logger = logging.getLogger("markgate")


def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def get_extension(filename: str) -> str:
    return FilePath(filename).suffix.lower()


async def background_update_s3(
    file_hash: str,
    version: Version,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> None:
    """
    Upload / Update file source, _aliases.json and _metadata.json on S3
    This function includes a double lock :
     - Depending on the hashfile
     - Depending on the hashfile + version
    """
    ext: str = get_extension(filename) or ".bin"
    source_key: str = f"documents/{file_hash}/source{ext}"
    alias_key: str = f"documents/{file_hash}/_aliases.json"
    meta_key: str = f"documents/{file_hash}/{version.value}/_metadata.json"
    now: datetime = datetime.now(tz=UTC)

    lock_name: str = f"lock:{file_hash}:{version.value}"

    async with (
        redis_manager.client.lock(
            f"lock:uploading_source_file:{file_hash}", timeout=600, blocking_timeout=20
        ),
        redis_manager.client.lock(lock_name, timeout=600, blocking_timeout=20),
    ):
        try:
            # Upload source file to S3 if new hash
            if not s3_key_exists(source_key):
                start_time = time.perf_counter()
                logger.info(
                    f"BG [{version.value}] | Uploading source file: {filename} | Hash: {file_hash}"
                )
                await s3_manager.client.put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=source_key,
                    Body=content,
                    ContentType=content_type,
                    Metadata={"original_name": filename},
                )
                duration = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"BG [{version.value}] | Uploaded to S3 | Duration: {duration:.2f}ms | File: {filename} | Hash: {file_hash}"
                )

            # Create / Update _aliases file
            if s3_key_exists(alias_key):
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
            if s3_key_exists(meta_key):
                meta = await s3_get_pydantic(meta_key, S3Metadata)
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
            await s3_put_pydantic(meta_key, meta)

        except Exception as e:
            logger.error(
                f"BG [{version.value}] | ERROR | Hash: {file_hash} | Error: {e}"
            )


async def update_s3_processed(
    processed_document: ProcessedDocument,
    s3_content_key: str,
    s3_metadata_key: str,
):
    """
    Upload the processed document: md extraction and metadata (from backend processor)
    This function should always be directly executed in the route (not a background task)
     and called within a lock depending on the hashfile + version
    """
    await s3_put_content(s3_content_key, processed_document.page_content)
    await s3_put_pydantic(s3_metadata_key, processed_document.metadata)


async def call_upstream_backend(
    url: str, content: bytes, headers: dict[str, str], params: dict[str, str | bool]
) -> ProcessedDocument:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.put(url, content=content, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
