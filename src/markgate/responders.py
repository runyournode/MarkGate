"""Shared request/response pipeline, used by both the static routes (api.py) and the dynamically
generated alias routes (alias_routes.py).

`run()` is the pure domain call: resolve the document (cache or upstream) via
services.resolve_request(). `as_md()` / `as_json_with_images()` / `as_archive()` are the
"responders" that turn its result into one of the three response shapes MarkGate exposes.
Mirrors foil-serve's api.py split (_run() / _as_json() / _as_archive()) — pulled into its own
module here rather than living alongside routes, since two different route sources (static
decorators in api.py, dynamically generated routes in alias_routes.py) both need it identically.
"""

import time
import asyncio
import logging
from typing import NamedTuple

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import Response

from contracts import ProcessingConfig
from media import build_tar_zst, batch_pil_to_bytes
from schemas import (
    ExternalDocumentRequestHeaders,
    ProcessedDocument,
    ProcessedDocumentOut,
    ResponseDocument,
)
from services import gather_images, resolve_request
from config.loader import Version

logger = logging.getLogger("markgate")


class ProcessResult(NamedTuple):
    """Everything a responder needs to shape a response — the equivalent of foil-serve's
    ProcessedResult, extended with what MarkGate's cache layer additionally exposes.
    """

    processed_document: ProcessedDocument
    from_cache: bool
    filename: str
    s3_imgs_key: str
    version: Version
    start_time: float


async def run(
    headers_data: ExternalDocumentRequestHeaders,
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: str,
    file_content: bytes,
    config: ProcessingConfig,
    force_reprocess: bool,
    route: str = "",
) -> ProcessResult:
    """Shared preamble for all process routes (base and alias): delegate to resolve_request()
    with an already-resolved config. Base routes (api.py) resolve it as
    `VERSION_CONFIGS[version].with_overrides(overrides)` right before calling this; alias routes
    (alias_routes.py) pass their pre-resolved ResolvedAlias.config instead — same resolution,
    just computed once at startup rather than per request."""
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
        config,
        background_tasks,
        api_key,
        file_content,
        force_reprocess,
        route=route,
    )
    return ProcessResult(
        processed_document, from_cache, filename, s3_imgs_key, version, start_time
    )


def log_resp(result: ProcessResult, label: str = "") -> None:
    duration = (time.perf_counter() - result.start_time) * 1000
    tag = f" | {label}" if label else ""
    logger.info(
        f"RESP [{result.version.value}]{tag} | Total: {duration:.0f} ms | File: {result.filename}"
    )


def as_md(result: ProcessResult) -> ResponseDocument:
    log_resp(result)
    return ResponseDocument(
        page_content=result.processed_document.page_content,
        metadata=result.processed_document.metadata,
    )


async def as_json_with_images(result: ProcessResult) -> ProcessedDocumentOut:
    try:
        images = await gather_images(
            result.processed_document,
            result.from_cache,
            result.s3_imgs_key,
            result.version,
            result.filename,
        )
    except Exception as e:
        logger.error(
            f"CACHE [{result.version.value}] | S3 image retrieval failed | File: {result.filename} | Error: {e}"
        )
        raise HTTPException(
            status_code=502,
            detail=f"[{result.version.value}] Failed to retrieve images for '{result.filename}': {e}",
        )

    log_resp(result)

    # ProcessedDocument.serialize_images (base64, original format) can run at serialization time
    # but can't be turned into a JSON Schema — go through model_dump(mode="json") to reuse it, and
    # land on ProcessedDocumentOut (images: dict[str, str]), the schema-safe public shape.
    document = ProcessedDocument(
        page_content=result.processed_document.page_content,
        images=images,
        metadata=result.processed_document.metadata,
    )
    return ProcessedDocumentOut(**document.model_dump(mode="json"))


async def as_archive(result: ProcessResult) -> Response:
    images_error: str | None = None
    try:
        images = await gather_images(
            result.processed_document,
            result.from_cache,
            result.s3_imgs_key,
            result.version,
            result.filename,
        )
    except Exception as e:
        logger.warning(
            f"CACHE [{result.version.value}] | DOWNLOAD | S3 image retrieval failed | File: {result.filename} | Error: {e}"
        )
        images = {}
        images_error = f"Image retrieval from S3 failed: {e}"

    images_bytes = await asyncio.to_thread(batch_pil_to_bytes, images)

    archive = await asyncio.to_thread(
        build_tar_zst,
        result.processed_document.page_content,
        images_bytes,
        result.processed_document.metadata,
        images_error,
    )

    log_resp(result, label="DOWNLOAD")

    return Response(
        content=archive,
        media_type="application/zstd",
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}.tar.zst"'
        },
    )
