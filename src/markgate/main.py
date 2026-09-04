import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Any, NamedTuple
from urllib.parse import urlencode

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi_offline import FastAPIOffline

from backends.foil import SpreadsheetOverrides
from config.settings import settings
from contracts import ProcessingConfig
from media import build_tar_zst, batch_pil_to_bytes
from schemas import (
    ExternalDocumentRequestHeaders,
    ProcessedDocument,
    ProcessedDocumentOut,
    ResponseDocument,
    ProxyOutput,
    ServiceHealth,
    DependenciesHealth,
)
from security import make_alias_api_key_verifier, verify_api_key
from services import check_backends_health, gather_images, resolve_request
from storage import check_redis_health, check_s3_health, lifespan
from config.loader import ALIAS_ROUTES, VERSION_CONFIGS, Version

# --- Logging Configuration ---
logger = logging.getLogger("markgate")
logger.setLevel(settings.log_level)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

if settings.log_file:
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
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


# ---------------------------------------------------------------------------
# Process routes
#
# All three routes below do the same thing — resolve the Version's config (with any per-request
# overrides applied) and delegate to resolve_request() — and differ only in how they shape the
# response. Mirrors foil-serve's api.py split: _run() is the pure domain call, _as_md()/
# _as_json_with_images()/_as_archive() are the "responders" that turn its result into a response.
# Unlike foil-serve there's no closure-factory here: with only 3 routes (not a mode × shape
# combinatorial explosion), a `Responder` type injected into a builder function would have nothing
# to earn its keep against.
# ---------------------------------------------------------------------------


class _ProcessResult(NamedTuple):
    """Everything a responder needs to shape a response — the equivalent of foil-serve's
    ProcessedResult, extended with what MarkGate's cache layer additionally exposes.
    """

    processed_document: ProcessedDocument
    from_cache: bool
    filename: str
    s3_imgs_key: str
    version: Version
    start_time: float


async def _run(
    headers_data: ExternalDocumentRequestHeaders,
    version: Version,
    background_tasks: BackgroundTasks,
    api_key: str,
    file_content: bytes,
    config: ProcessingConfig,
    force_reprocess: bool,
    route: str = "",
) -> _ProcessResult:
    """Shared preamble for all process routes (base and alias): delegate to resolve_request()
    with an already-resolved config. Base routes resolve it as
    `VERSION_CONFIGS[version].with_overrides(overrides)` right before calling this; alias routes
    (see _register_alias_routes()) pass their pre-resolved ResolvedAlias.config instead — same
    resolution, just computed once at startup rather than per request."""
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
    return _ProcessResult(
        processed_document, from_cache, filename, s3_imgs_key, version, start_time
    )


def _log_resp(result: _ProcessResult, label: str = "") -> None:
    duration = (time.perf_counter() - result.start_time) * 1000
    tag = f" | {label}" if label else ""
    logger.info(
        f"RESP [{result.version.value}]{tag} | Total: {duration:.0f} ms | File: {result.filename}"
    )


def _as_md(result: _ProcessResult) -> ResponseDocument:
    _log_resp(result)
    return ResponseDocument(
        page_content=result.processed_document.page_content,
        metadata=result.processed_document.metadata,
    )


async def _as_json_with_images(result: _ProcessResult) -> ProcessedDocumentOut:
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

    _log_resp(result)

    # ProcessedDocument.serialize_images (base64, original format) can run at serialization time
    # but can't be turned into a JSON Schema — go through model_dump(mode="json") to reuse it, and
    # land on ProcessedDocumentOut (images: dict[str, str]), the schema-safe public shape.
    document = ProcessedDocument(
        page_content=result.processed_document.page_content,
        images=images,
        metadata=result.processed_document.metadata,
    )
    return ProcessedDocumentOut(**document.model_dump(mode="json"))


async def _as_archive(result: _ProcessResult) -> Response:
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

    _log_resp(result, label="DOWNLOAD")

    return Response(
        content=archive,
        media_type="application/zstd",
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}.tar.zst"'
        },
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
    overrides: Annotated[SpreadsheetOverrides, Depends(SpreadsheetOverrides.as_query)],
    force_reprocess: bool = Query(False),
) -> ProxyOutput | dict:
    """Convert a document to Markdown. Returns page_content and metadata — no images.
    See PUT /{version}/process for a variant that includes images.
    """
    config = VERSION_CONFIGS[version].with_overrides(overrides)
    return _as_md(
        await _run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
        )
    )


@app.put(
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
    return await _as_json_with_images(
        await _run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
        )
    )


@app.put(
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
    return await _as_archive(
        await _run(
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
# Route aliases
#
# Static path aliases declared in backend_config.toml (see backends.foil.FoilRouteAlias) — for
# clients that can't set query params. Each alias resolves to an already-merged ProcessingConfig,
# computed once at import time in config/loader.py's _resolve_aliases(), so these routes are
# functionally identical to their query-param equivalent on the parent Version, including sharing
# its cache entry / processing lock (see FoilConfig.cache_key()). Registered here with
# add_api_route() rather than as @app.put decorators, since the set of aliases is only known once
# backend_config.toml has been loaded — one route triple per declared alias, mirroring the three
# @app.put routes above, all under a fixed `/alias` prefix so every alias route is recognizable at
# a glance and can't collide with a real Version name. `{alias.name}` always sits between
# `{version}` and `process` (not after) so every alias path still ends in /process or
# /process/download.
#
# Each handler factory below binds `version`/`config`/`verify` as default-argument values (not
# free variables) specifically to avoid the classic Python closure-in-a-loop bug, where every
# closure would otherwise share the loop's final `alias`.
# ---------------------------------------------------------------------------


def _alias_equivalent_description(equivalent_path: str, overrides: Any) -> str:
    """Build an OpenAPI `description` spelling out the exact query-param URL an alias route
    resolves to — e.g. "equivalent to PUT /md/foil/process?spreadsheet_mode=ocr" — rather than
    just pointing at backend_config.toml, so it's readable straight from /docs."""
    query_string = urlencode(
        {k: str(v) for k, v in overrides.model_dump(exclude_none=True).items()}
    )
    equivalent_url = (
        f"{equivalent_path}?{query_string}" if query_string else equivalent_path
    )
    return (
        f"Static alias — equivalent to `PUT {equivalent_url}`. Resolves to the exact same call "
        "— and shares the same cache entry / processing lock — as that query-param request (see "
        "[[backends.*.aliases]] in backend_config.toml)."
    )


def _register_alias_routes(app: FastAPI) -> None:
    for alias in ALIAS_ROUTES:
        verify = make_alias_api_key_verifier(alias.version)
        base_path = f"{alias.version.value}/{alias.name}/process"
        md_description = _alias_equivalent_description(
            f"/md/{alias.version.value}/process", alias.overrides
        )
        images_description = _alias_equivalent_description(
            f"/{alias.version.value}/process", alias.overrides
        )
        download_description = _alias_equivalent_description(
            f"/{alias.version.value}/process/download", alias.overrides
        )

        def make_md_handler(
            version: Version = alias.version,
            config: ProcessingConfig = alias.config,
            verify=verify,
        ):
            async def handler(
                headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
                background_tasks: BackgroundTasks,
                api_key: Annotated[str, Depends(verify)],
                file_content: Annotated[
                    bytes, Body(media_type="application/octet-stream")
                ],
                force_reprocess: bool = Query(False),
            ) -> ProxyOutput | dict:
                return _as_md(
                    await _run(
                        headers_data,
                        version,
                        background_tasks,
                        api_key,
                        file_content,
                        config,
                        force_reprocess,
                    )
                )

            return handler

        def make_images_handler(
            version: Version = alias.version,
            config: ProcessingConfig = alias.config,
            verify=verify,
        ):
            async def handler(
                headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
                background_tasks: BackgroundTasks,
                api_key: Annotated[str, Depends(verify)],
                file_content: Annotated[
                    bytes, Body(media_type="application/octet-stream")
                ],
                force_reprocess: bool = Query(False),
            ) -> ProcessedDocumentOut:
                return await _as_json_with_images(
                    await _run(
                        headers_data,
                        version,
                        background_tasks,
                        api_key,
                        file_content,
                        config,
                        force_reprocess,
                    )
                )

            return handler

        def make_download_handler(
            version: Version = alias.version,
            config: ProcessingConfig = alias.config,
            verify=verify,
        ):
            async def handler(
                headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
                background_tasks: BackgroundTasks,
                api_key: Annotated[str, Depends(verify)],
                file_content: Annotated[
                    bytes, Body(media_type="application/octet-stream")
                ],
                force_reprocess: bool = Query(False),
            ) -> Response:
                return await _as_archive(
                    await _run(
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

            return handler

        app.add_api_route(
            f"/alias/md/{base_path}",
            make_md_handler(),
            methods=["PUT"],
            response_model=ProxyOutput,
            description=md_description,
        )
        app.add_api_route(
            f"/alias/{base_path}",
            make_images_handler(),
            methods=["PUT"],
            response_model=ProcessedDocumentOut,
            description=images_description,
        )
        app.add_api_route(
            f"/alias/{base_path}/download",
            make_download_handler(),
            methods=["PUT"],
            response_class=Response,
            responses={
                200: {
                    "content": {"application/zstd": {}},
                    "description": "tar.zst archive",
                }
            },
            description=download_description,
        )


_register_alias_routes(app)


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Health"])
async def health():
    """Application liveness: returns 200 if the app is running."""
    return {"status": "ok"}


@app.get("/health/dependencies", response_model=DependenciesHealth, tags=["Health"])
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the favicon."""
    return FileResponse(STATICS_DIR / "favicon.ico", media_type="image/x-icon")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
