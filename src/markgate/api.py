"""HTTP surface: the 3 static process routes, image lookup, health endpoints, and the favicon — every route
that's known at code-writing time, declared with @router.put/@router.get decorators. Mirrors
foil-serve's api.py (a `router = APIRouter()` holding all the routes). Dynamically generated
routes (one triple per backend_config.toml alias, count unknown until the config is loaded) live
separately in alias_routes.py — see that module's docstring for why.

All three routes below do the same thing — resolve the Version's config (with any per-request
overrides applied) and delegate to responders.run() — and differ only in how they shape the
response via responders.as_md() / as_json_with_images() / as_archive().
"""

import base64
import logging
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
)
from fastapi.responses import JSONResponse, FileResponse, Response

import responders
from backends.foil import SpreadsheetOverrides
from media import image_mime
from schemas import (
    ExternalDocumentRequestHeaders,
    ImagePayload,
    ImagesRequest,
    ImagesResponse,
    ProcessedDocumentOut,
    ProxyOutput,
    ServiceHealth,
    DependenciesHealth,
)
from security import verify_api_key, verify_api_key_images
from services import check_backends_health, get_images_by_name
from storage import check_redis_health, check_s3_health, s3_is_active
from config.loader import VERSION_CONFIGS, Version

logger = logging.getLogger("markgate")

router = APIRouter()

STATICS_DIR: Path = Path(__file__).resolve().parent / "statics"


# ---------------------------------------------------------------------------
# Process routes
# ---------------------------------------------------------------------------


@router.put(
    "/md/{version}/process",
    response_model=ProxyOutput,
)
async def process_document(
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: Annotated[str, Depends(verify_api_key)],
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    overrides: Annotated[SpreadsheetOverrides, Depends(SpreadsheetOverrides.as_query)],
    force_reprocess: bool = Query(False),
) -> ProxyOutput | dict:
    """Convert a document to Markdown. Returns page_content and metadata — no images.
    See PUT /{version}/process for a variant that includes images.
    """
    config = VERSION_CONFIGS[version].with_overrides(overrides)
    return responders.as_md(
        await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
        )
    )


@router.put(
    "/{version}/process",
    response_model=ProcessedDocumentOut,
)
async def process_document_with_images(
    headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: Annotated[str, Depends(verify_api_key)],
    file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    overrides: Annotated[SpreadsheetOverrides, Depends(SpreadsheetOverrides.as_query)],
    force_reprocess: bool = Query(False),
) -> ProcessedDocumentOut:
    """Convert a document to Markdown. Returns page_content, metadata and images (base64)."""
    config = VERSION_CONFIGS[version].with_overrides(overrides)
    return await responders.as_json_with_images(
        await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
        )
    )


@router.put(
    "/{version}/process/download",
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
    overrides: Annotated[SpreadsheetOverrides, Depends(SpreadsheetOverrides.as_query)],
    force_reprocess: bool = Query(False),
) -> Response:
    """Convert a document to Markdown and return a tar.zst archive (content.md, images and metadata)."""
    config = VERSION_CONFIGS[version].with_overrides(overrides)
    return await responders.as_archive(
        await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
            route="DOWNLOAD",
        )
    )


# ---------------------------------------------------------------------------
# Image lookup
# ---------------------------------------------------------------------------


@router.post("/images", response_model=ImagesResponse, tags=["Images"])
async def get_images(
    request: ImagesRequest,
    api_key: Annotated[str, Depends(verify_api_key_images)],
) -> ImagesResponse:
    """Return images of an already processed document from the cache, by name.

    `engine` and `source_sha256` are the `extraction_engine` / `source_sha256` metadata of the
    process responses; `names` are image paths as written in the Markdown. Only backends whose
    images are named after their content support this lookup (foil, docling).
    """
    config = next(
        (cfg for cfg in VERSION_CONFIGS.values() if cfg.backend_type == request.engine),
        None,
    )
    if config is None or not config.content_addressed_images:
        raise HTTPException(
            status_code=422,
            detail=f"Engine '{request.engine}' does not support image lookup.",
        )
    names: list[str] = []
    for raw_name in request.names:
        name = config.normalize_image_name(raw_name)
        if name is None:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid image name for engine '{request.engine}': {raw_name!r}",
            )
        if name not in names:
            names.append(name)

    if not s3_is_active():
        raise HTTPException(
            status_code=503, detail="Image cache (S3) is not available."
        )
    try:
        images, missing = await get_images_by_name(request.source_sha256, config, names)
    except Exception as e:
        logger.error(
            f"IMAGES [{request.engine}] | S3 read failed | Hash: {request.source_sha256} | Error: {e}"
        )
        raise HTTPException(status_code=502, detail="Image retrieval from S3 failed.")

    logger.info(
        f"IMAGES [{request.engine}] | {len(images)} found, {len(missing)} missing | "
        f"Hash: {request.source_sha256} | ClientKey: {api_key[:4]}***"
    )
    return ImagesResponse(
        images={
            name: ImagePayload(
                mime=image_mime(name), data=base64.b64encode(data).decode("ascii")
            )
            for name, data in images.items()
        },
        missing=missing,
    )


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


@router.get("/health", tags=["Health"])
async def health():
    """Application liveness: returns 200 if the app is running."""
    return {"status": "ok"}


@router.get("/health/dependencies", response_model=DependenciesHealth, tags=["Health"])
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


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the favicon."""
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")
