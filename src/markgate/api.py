"""HTTP surface: the 3 static process routes, health endpoints, and the favicon — every route
that's known at code-writing time, declared with @router.put/@router.get decorators. Mirrors
foil-serve's api.py (a `router = APIRouter()` holding all the routes). Dynamically generated
routes (one triple per backend_config.toml alias, count unknown until the config is loaded) live
separately in alias_routes.py — see that module's docstring for why.

All three routes below do the same thing — resolve the Version's config (with any per-request
overrides applied) and delegate to responders.run() — and differ only in how they shape the
response via responders.as_md() / as_json_with_images() / as_archive().
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, Query
from fastapi.responses import JSONResponse, FileResponse, Response

import responders
from backends.foil import SpreadsheetOverrides
from schemas import (
    ExternalDocumentRequestHeaders,
    ProcessedDocumentOut,
    ProxyOutput,
    ServiceHealth,
    DependenciesHealth,
)
from security import verify_api_key
from services import check_backends_health
from storage import check_redis_health, check_s3_health
from config.loader import VERSION_CONFIGS, Version

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
