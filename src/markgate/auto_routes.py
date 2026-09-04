"""Optional /auto/* routes: /md/auto/process, /auto/process, /auto/process/download.

Mirrors alias_routes.py's shape (`build_router() -> APIRouter | None`, built once) rather than
living in api.py: like alias_routes.py, this is a self-contained, optional set of routes with its
own startup-time loading step (here, loading the AUTO_SELECTOR_PATH file — see routing.py), so
mixing it into api.py's fixed set of statically-known routes would make both harder to read.

Unlike the explicit routes, the client doesn't name a backend — build_router() wires a
`select(ctx) -> BackendSelection` function (loaded via routing.load_selector()) to decide it from
the uploaded file. Reuses responders.run() / as_md() / as_json_with_images() / as_archive() exactly
like api.py and alias_routes.py do, so caching, locking and upstream dispatch are entirely
unaffected by auto-selection — see the cache/lock note below.

Cache/lock coherence: selection is a pure, local, in-memory decision — no Redis, no S3, no upstream
call — and it runs *before* resolve_request() builds the hash/cache-key/lock (a Version is a
required input to config.cache_key()). Once resolved, these routes call the identical
responders.run() -> resolve_request() pipeline as the explicit routes, so the exact same
(Version, overrides) pair always converges on the exact same S3 entry / Redis lock regardless of
which route produced it — this already held for "explicit Version" vs. "explicit Version +
query-param override" vs. "TOML alias" (all funnel through with_overrides() -> cache_key(), see
FoilConfig.cache_key()); /auto/* is simply a fourth way to reach that same pair.

Route registration order: main.py must call app.include_router(auto_routes.build_router()) (when
not None) *before* app.include_router(api.router) — Starlette matches routes by path shape in
registration order and won't fall through to a later route once one has matched, so api.router's
`/{version}/process` would otherwise path-match a literal request to `/auto/process` and fail
(since "auto" isn't a Version) before this module's literal route is ever tried. See main.py.
"""

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
from fastapi.responses import Response

import responders
from config.loader import VERSION_CONFIGS
from config.settings import settings
from media import get_mime_type
from routing import (
    BackendSelection,
    BackendSelectionContext,
    NoSuitableBackendError,
    load_selector,
)
from schemas import ExternalDocumentRequestHeaders, ProcessedDocumentOut, ProxyOutput
from security import verify_api_key_auto


def build_router() -> APIRouter | None:
    """Return the 3 /auto/* routes, or None if auto_route_enabled is false. Loads the selector
    file once here (fail-fast on a broken AUTO_SELECTOR_PATH), mirroring config/loader.py's
    module-level _load()."""
    if not settings.auto_route_enabled:
        return None

    select_fn = load_selector()
    router = APIRouter()

    async def resolve_auto_selection(
        headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
        file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
    ) -> BackendSelection:
        if (
            settings.max_upload_size_bytes is not None
            and len(file_content) > settings.max_upload_size_bytes
        ):
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds max_upload_size_bytes ({settings.max_upload_size_bytes} bytes).",
            )
        ctx = BackendSelectionContext(
            file_content=file_content,
            filename=headers_data.filename,
            extension=Path(headers_data.filename).suffix.lower(),
            declared_content_type=headers_data.content_type,
            sniffed_mime=get_mime_type(file_content),
            size_bytes=len(file_content),
            available_versions=VERSION_CONFIGS,
        )
        try:
            return await select_fn(ctx)
        except NoSuitableBackendError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @router.put("/md/auto/process", response_model=ProxyOutput)
    async def process_document_auto(
        headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        selection: Annotated[BackendSelection, Depends(resolve_auto_selection)],
        background_tasks: BackgroundTasks,
        file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
        response: Response,
        force_reprocess: bool = Query(False),
    ) -> ProxyOutput | dict:
        """Auto-selects a backend from the uploaded file, then converts it to Markdown. Returns
        page_content and metadata — no images. The resolved backend is reported in the
        X-Resolved-Backend response header."""
        version = selection.version
        config = VERSION_CONFIGS[version].with_overrides(selection.overrides)
        result = await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
            route="AUTO",
        )
        response.headers["X-Resolved-Backend"] = version.value
        return responders.as_md(result)

    @router.put("/auto/process", response_model=ProcessedDocumentOut)
    async def process_document_with_images_auto(
        headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        selection: Annotated[BackendSelection, Depends(resolve_auto_selection)],
        background_tasks: BackgroundTasks,
        file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
        response: Response,
        force_reprocess: bool = Query(False),
    ) -> ProcessedDocumentOut:
        """Auto-selects a backend from the uploaded file, then converts it to Markdown. Returns
        page_content, metadata and images (base64). The resolved backend is reported in the
        X-Resolved-Backend response header."""
        version = selection.version
        config = VERSION_CONFIGS[version].with_overrides(selection.overrides)
        result = await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
        )
        response.headers["X-Resolved-Backend"] = version.value
        return await responders.as_json_with_images(result)

    @router.put(
        "/auto/process/download",
        response_class=Response,
        responses={
            200: {"content": {"application/zstd": {}}, "description": "tar.zst archive"}
        },
    )
    async def process_document_download_auto(
        headers_data: Annotated[ExternalDocumentRequestHeaders, Header()],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        selection: Annotated[BackendSelection, Depends(resolve_auto_selection)],
        background_tasks: BackgroundTasks,
        file_content: Annotated[bytes, Body(media_type="application/octet-stream")],
        force_reprocess: bool = Query(False),
    ) -> Response:
        """Auto-selects a backend from the uploaded file, converts it to Markdown, and returns a
        tar.zst archive (content.md, images and metadata). The resolved backend is reported in
        the X-Resolved-Backend response header."""
        version = selection.version
        config = VERSION_CONFIGS[version].with_overrides(selection.overrides)
        result = await responders.run(
            headers_data,
            version,
            background_tasks,
            api_key,
            file_content,
            config,
            force_reprocess,
            route="AUTO/DOWNLOAD",
        )
        archive_response = await responders.as_archive(result)
        archive_response.headers["X-Resolved-Backend"] = version.value
        return archive_response

    return router
