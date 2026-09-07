"""Optional /auto/* routes: /md/auto/process, /auto/process, /auto/process/download.

Mirrors alias_routes.py's shape (`build_router() -> APIRouter | None`, built once), kept out of
api.py for the same reason: its own startup-time loading step (the AUTO_SELECTOR_PATH file, see
routing.py). Here the client doesn't name a backend — build_router() wires a
`select(ctx) -> BackendSelection` function (routing.load_selector()) to pick one from the
uploaded file, then reuses responders.run()/as_md()/as_json_with_images()/as_archive() exactly
like api.py and alias_routes.py, so caching, locking and upstream dispatch are unaffected by
auto-selection.

Selection runs before resolve_request() builds the hash/cache-key/lock, so a given
(Version, overrides) pair always converges on the same S3 entry / Redis lock regardless of
whether it came from an explicit Version, a query-param override, a TOML alias, or /auto/*.

Route registration order: main.py must include auto_routes.build_router() *before* api.router —
Starlette matches routes by path shape in registration order, so api.router's
`/{version}/process` would otherwise shadow a literal `/auto/process` request first (since
"auto" isn't a Version) and reject it before this module's route is ever tried.
"""

from pathlib import Path
from typing import Annotated, NamedTuple

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


class AutoResolution(NamedTuple):
    """resolve_auto_selection()'s result: the selector's decision plus the request data it was
    computed from — bundled so headers_data/file_content are each declared as a FastAPI param
    exactly once (in the dependency below), not duplicated in every route's own signature. A
    Header()-typed Pydantic model declared twice in one dependency tree makes FastAPI collapse
    it into one opaque header field in the OpenAPI schema instead of exploding it into
    Content-Type/X-Filename — breaking the /docs "Try it out" form (requests sent directly are
    unaffected)."""

    selection: BackendSelection
    headers_data: ExternalDocumentRequestHeaders
    file_content: bytes


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
    ) -> AutoResolution:
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
            selection = await select_fn(ctx)
        except NoSuitableBackendError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return AutoResolution(selection, headers_data, file_content)

    @router.put("/md/auto/process", response_model=ProxyOutput)
    async def process_document_auto(
        resolution: Annotated[AutoResolution, Depends(resolve_auto_selection)],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        background_tasks: BackgroundTasks,
        response: Response,
        force_reprocess: bool = Query(False),
    ) -> ProxyOutput | dict:
        """Auto-selects a backend from the uploaded file, then converts it to Markdown. Returns
        page_content and metadata — no images. The resolved backend is reported in the
        X-Resolved-Backend response header."""
        version = resolution.selection.version
        config = VERSION_CONFIGS[version].with_overrides(resolution.selection.overrides)
        result = await responders.run(
            resolution.headers_data,
            version,
            background_tasks,
            api_key,
            resolution.file_content,
            config,
            force_reprocess,
            route="AUTO",
        )
        response.headers["X-Resolved-Backend"] = version.value
        return responders.as_md(result)

    @router.put("/auto/process", response_model=ProcessedDocumentOut)
    async def process_document_with_images_auto(
        resolution: Annotated[AutoResolution, Depends(resolve_auto_selection)],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        background_tasks: BackgroundTasks,
        response: Response,
        force_reprocess: bool = Query(False),
    ) -> ProcessedDocumentOut:
        """Auto-selects a backend from the uploaded file, then converts it to Markdown. Returns
        page_content, metadata and images (base64). The resolved backend is reported in the
        X-Resolved-Backend response header."""
        version = resolution.selection.version
        config = VERSION_CONFIGS[version].with_overrides(resolution.selection.overrides)
        result = await responders.run(
            resolution.headers_data,
            version,
            background_tasks,
            api_key,
            resolution.file_content,
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
        resolution: Annotated[AutoResolution, Depends(resolve_auto_selection)],
        api_key: Annotated[str, Depends(verify_api_key_auto)],
        background_tasks: BackgroundTasks,
        force_reprocess: bool = Query(False),
    ) -> Response:
        """Auto-selects a backend from the uploaded file, converts it to Markdown, and returns a
        tar.zst archive (content.md, images and metadata). The resolved backend is reported in
        the X-Resolved-Backend response header."""
        version = resolution.selection.version
        config = VERSION_CONFIGS[version].with_overrides(resolution.selection.overrides)
        result = await responders.run(
            resolution.headers_data,
            version,
            background_tasks,
            api_key,
            resolution.file_content,
            config,
            force_reprocess,
            route="AUTO/DOWNLOAD",
        )
        archive_response = await responders.as_archive(result)
        archive_response.headers["X-Resolved-Backend"] = version.value
        return archive_response

    return router
