"""WebUI served on /ui when `enable_ui` is true (see main.py). Everything UI-related lives in this
package: the static single-page app (static/) and the one endpoint it needs that the OpenAPI
document can't provide — a filtered, non-secret view of backend_config.toml.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from backends.foil import SpreadsheetOverrides
from config.loader import VERSION_CONFIGS

STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

# Only the per-request overridable params are published, with each Version's configured value.
# Allow-list on purpose: backend configs also hold upstream URLs, headers and API keys.
_OVERRIDABLE_PARAMS = tuple(SpreadsheetOverrides.model_fields)

router = APIRouter()


@router.get("/ui/backends.json", include_in_schema=False)
async def backends() -> dict[str, dict[str, Any]]:
    """Backend type and overridable param defaults of every Version, used by the UI to hide
    parameters a backend would ignore."""
    result: dict[str, dict[str, Any]] = {}
    for version, config in VERSION_CONFIGS.items():
        query_params = getattr(config, "query_params", None)
        result[version.value] = {
            "backend_type": config.backend_type,
            "defaults": {
                name: getattr(query_params, name)
                for name in _OVERRIDABLE_PARAMS
                if hasattr(query_params, name)
            },
        }
    return result
