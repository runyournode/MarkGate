"""Shared contract between config/ and backends/.

ProcessingConfig is the base class for all backend-specific config models.
It lives here — outside both packages — to break the mutual dependency:
  config/loader.py  needs BackendConfig  (uses FoilConfig | DoclingConfig)
  backends/*.py     needs ProcessingConfig (base class)
"""

import os
import re
from typing import Any

from pydantic import BaseModel


class ProcessingConfig(BaseModel):
    """Base fields shared by all processing backends."""

    backend_type: str
    """Handler key — must match a registered entry in backends.BACKEND_HANDLERS."""

    description: str
    """Internal description, not exposed to clients."""

    upstream_url: str
    """Full URL of the upstream processing backend."""

    authorized_api_key: str
    """Bearer token expected from clients calling this version."""

    custom_headers: dict[str, str] = {}
    """Headers forwarded as-is to the upstream backend (e.g. Authorization)."""

    cache_id: str | None = None
    """Stable S3 path key for this backend version.
    If set, S3 paths use this value instead of the version name.
    Set to the old version name when renaming a version to preserve existing cache.
    """

    def get_raw_query_params(self) -> dict[str, Any]:
        """Return query/form params as a plain dict, ready for the backend handler."""
        return {}


def resolve_env_placeholders(value: Any) -> Any:
    """Recursively replace ``${VAR_NAME}`` with the corresponding os.environ value."""
    if isinstance(value, str):
        def _replace(m: re.Match[str]) -> str:
            var = m.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                raise ValueError(
                    f"Environment variable '{var}' referenced in backend_config.toml is not set."
                )
            return resolved

        return re.sub(r"\$\{([^}]+)\}", _replace, value)
    if isinstance(value, dict):
        return {k: resolve_env_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_placeholders(v) for v in value]
    return value