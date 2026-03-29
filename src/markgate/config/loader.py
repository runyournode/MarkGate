"""Runtime backend registry: loads backend_config.toml and exposes Version + VERSION_CONFIGS.

Imported once at app startup. The local import of BackendConfig inside _load() is
intentional: loader.py is itself a submodule of config/, and a top-level import of
backends would trigger backends/__init__.py before config.settings is ready on some
import paths. The local import defers it to after all config submodules are initialized.
"""

import tomllib
from enum import Enum
from pathlib import Path
from typing import Any

from contracts import ProcessingConfig, resolve_env_placeholders  # noqa: F401
from config.settings import settings


def _load() -> tuple[type, dict[Any, ProcessingConfig]]:
    from backends import BackendConfig

    backends_path = Path(settings.backend_config_path)
    if not backends_path.exists():
        raise FileNotFoundError(
            f"backend_config.toml not found at '{backends_path.resolve()}'. "
            "Create the file or set BACKEND_CONFIG_PATH to its location."
        )

    with backends_path.open("rb") as f:
        raw = tomllib.load(f)

    raw_backends: dict[str, dict[str, Any]] = raw.get("backends", {})
    if not raw_backends:
        raise ValueError(
            "backend_config.toml must define at least one entry under [backends.*]"
        )

    data = BackendConfig(backends=resolve_env_placeholders(raw_backends))

    # Dynamic str+Enum: names are Python-safe identifiers, values are the raw backend name strings.
    # Example: [backends.foil-m3b] → Version.foil_m3b, Version.foil_m3b.value == "foil-m3b"
    Version = Enum(
        "Version",
        {name.replace(".", "_").replace("-", "_"): name for name in data.backends},
        type=str,
    )
    VERSION_CONFIGS: dict[Any, ProcessingConfig] = {
        Version(name): cfg for name, cfg in data.backends.items()
    }
    return Version, VERSION_CONFIGS


Version, VERSION_CONFIGS = _load()