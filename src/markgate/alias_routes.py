"""Dynamically generated routes: one triple per backend_config.toml alias declaration (see
backends.foil.FoilRouteAlias), built from ALIAS_ROUTES — resolved and validated at import time in
config/loader.py. Kept separate from api.py's statically decorated routes: the number of routes
here isn't known until backend_config.toml has been loaded, generated in a loop via
add_api_route() rather than @router decorators, and each route needs its own closure over a
per-alias (version, config) pair. Mixing this generator with api.py's fixed set of decorated
routes would make both harder to read — see the module docstring in api.py.

Each alias resolves to an already-merged ProcessingConfig, computed once at import time in
config/loader.py's _resolve_aliases(), so these routes are functionally identical to their
query-param equivalent on the parent Version, including sharing its cache entry / processing lock
(see FoilConfig.cache_key()). `{alias.name}` always sits between `{version}` and `process` (not
after) so every alias path still ends in /process or /process/download, and every alias route
lives under a fixed `/alias` prefix so it's recognizable at a glance and can't collide with a real
Version name.

Each handler factory below binds `version`/`config`/`verify` as default-argument values (not free
variables) specifically to avoid the classic Python closure-in-a-loop bug, where every closure
would otherwise share the loop's final `alias`.
"""

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header, Query
from fastapi.responses import Response

import responders
from contracts import ProcessingConfig
from schemas import ExternalDocumentRequestHeaders, ProcessedDocumentOut, ProxyOutput
from security import make_alias_api_key_verifier
from config.loader import ALIAS_ROUTES, Version


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


def build_router() -> APIRouter:
    """Build an APIRouter holding the 3 routes for every alias in ALIAS_ROUTES."""
    router = APIRouter()

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

            return handler

        router.add_api_route(
            f"/alias/md/{base_path}",
            make_md_handler(),
            methods=["PUT"],
            response_model=ProxyOutput,
            description=md_description,
        )
        router.add_api_route(
            f"/alias/{base_path}",
            make_images_handler(),
            methods=["PUT"],
            response_model=ProcessedDocumentOut,
            description=images_description,
        )
        router.add_api_route(
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

    return router
