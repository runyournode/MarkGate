import logging
from datetime import UTC, datetime
from pathlib import Path as FilePath
import hashlib
import time
from urllib.parse import quote

from PIL import Image
import httpx

from config import VERSION_CONFIGS, ProcessingConfig, Version, settings
from schemas import S3Metadata, S3FileAliases, ProcessedDocument, Metadata
from utils import (
    s3_manager,
    s3_put_content,
    s3_put_imgs,
    s3_key_exists,
    s3_get_pydantic,
    s3_put_pydantic,
    redis_manager,
    get_mime_type,
    base64_to_pil,
)

logger = logging.getLogger("markgate")


def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def get_extension(filename: str) -> str:
    return FilePath(filename).suffix.lower()


def get_lock_name(file_hash: str, version: Version) -> str:
    return f"lock:{file_hash}:{version.value}"


async def background_update_s3(
    file_hash: str,
    version: Version,
    filename: str,
    content: bytes,
    content_type: str | None,
) -> None:
    """
    Upload / Update file source, _aliases.json and _metadata.json on S3
    Processed document is out of scope (see update_s3_processed)
    This function includes a double lock :
     - Depending on the hashfile
     - Depending on the hashfile + version
    """
    ext: str = get_extension(filename) or ".bin"
    source_key: str = f"documents/{file_hash}/source{ext}"
    alias_key: str = f"documents/{file_hash}/_aliases.json"
    meta_key: str = f"documents/{file_hash}/{version.value}/_metadata.json"
    now: datetime = datetime.now(tz=UTC)

    lock_name_unversioned: str = f"lock:uploading_source_file:{file_hash}"
    lock_name_versioned: str = get_lock_name(file_hash, version)

    async with (
        redis_manager.client.lock(
            lock_name_unversioned, timeout=600, blocking_timeout=20
        ),
        redis_manager.client.lock(
            lock_name_versioned, timeout=600, blocking_timeout=20
        ),
    ):
        try:
            # Upload source file to S3 if new hash
            if not await s3_key_exists(source_key):
                start_time = time.perf_counter()
                logger.info(
                    f"BG [{version.value}] | Uploading source file: {filename} | Hash: {file_hash}"
                )
                await s3_manager.client.put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=source_key,
                    Body=content,
                    ContentType=content_type,
                    Metadata={"original_name": quote(filename)},
                    ContentDisposition=f"attachment; filename*=UTF-8''{quote(filename)}",
                )
                duration = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"BG [{version.value}] | Uploaded to S3 | Duration: {duration:.2f}ms | File: {filename} | Hash: {file_hash}"
                )

            # Create / Update _aliases file
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
            if await s3_key_exists(meta_key):
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
    s3_imgs_key: str
):
    """
    Upload the processed document: md extraction, metadata (from backend processor) and optionnaly images
    This function should always be directly executed in the route (not a background task)
     and called within a lock depending on the hashfile + version
    """
    await s3_put_content(s3_content_key, processed_document.page_content)
    # metadata and images are optional
    if processed_document.metadata:
        await s3_put_pydantic(s3_metadata_key, processed_document.metadata)
    if processed_document.images:
        await s3_put_imgs(s3_imgs_key, processed_document.images)


async def call_upstream_backend(
    version: Version, file_content: bytes, headers: dict[str, str], filename: str
) -> ProcessedDocument:
    async with httpx.AsyncClient(timeout=300.0) as async_client:
    # with httpx.Client(timeout=300.0) as client:
        # Get the config
        config: ProcessingConfig = VERSION_CONFIGS[version]

        # add more headers from config (e.g. secret key)
        # if config.custom_headers:  # api key(s) for the backend
        #     headers.update(config.custom_headers)
        match version:
            case ( # routage vers paddleocrvl_server
                Version.v_1_0_0 | Version.v_1_1_0
            ):
                resp = await async_client.post(
                    url=config.upstream_url,
                    content=file_content,
                    params=config.query_params, # {} pour v1
                    headers=config.custom_headers,
                )
                resp.raise_for_status()
                data = resp.json()

                # Get md and dict of images (b64) from response
                page_content = data.get("page_content", "")
                imgs: dict[str, str] = data.get("images", {})

                # Convert to pil
                imgs: dict[str, Image.Image] = {name: base64_to_pil(img) for name, img in imgs.items()}

                return ProcessedDocument(
                    page_content=page_content,
                    images=imgs,
                    metadata=Metadata(), # empty for paddleocrvl_server
                )


            case Version.v_4_0_0:  # routage vers docling
                # files = {"files": (filename, file_content, headers["Content-Type"])}

                content_type = headers["Content-Type"]
                if content_type == 'application/octet-stream':
                    content_type = get_mime_type(file_content)


                files = {"files": (filename, file_content, content_type)}

                # debug
                # url = "http://localhost:5001/v1/convert/file"
                # parameters = {
                #     "from_formats": [
                #         "docx",
                #         "pptx",
                #         "html",
                #         "image",
                #         "pdf",
                #         "asciidoc",
                #         "md",
                #         "xlsx",
                #     ],
                #     "to_formats": ["md", "json", "html", "text", "doctags"],
                #     "image_export_mode": "placeholder",
                #     "do_ocr": True,
                #     "force_ocr": False,
                #     "ocr_engine": "easyocr",
                #     "ocr_lang": ["en"],
                #     "pdf_backend": "dlparse_v2",
                #     "table_mode": "fast",
                #     "abort_on_error": False,
                # }
                # resp = await async_client.post(url, files=files, data=parameters, headers=config.custom_headers)

                resp = await async_client.post(
                    url=config.upstream_url,
                    files=files,
                    data=config.query_params,
                    headers=config.custom_headers,
                )
                resp.raise_for_status()

                data = resp.json()

                page_content = data.get("document", {}).get("md_content", "")

                return ProcessedDocument(
                    page_content=page_content,
                    metadata=Metadata(
                        status=data.get("status"),
                        processing_time=data.get("processing_time"),
                        errors=data.get("errors"),
                    ),
                    images={}
                )

            case _:
                # Standard PUT for other backends (Marker, etc.)
                raise NotImplementedError()
