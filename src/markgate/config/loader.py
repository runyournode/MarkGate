"""Runtime backend registry: loads backend_config.toml and exposes Version + VERSION_CONFIGS.

## Why dynamic ?

Backend versions are declared in backend_config.toml, not hardcoded in Python.
Adding or renaming a backend only requires editing the TOML — no code change needed.

To achieve this, `Version` is not a regular class: it is built at startup by Python's
`Enum()` functional API from the list of backend names found in the TOML file.

    [backends.foil]           →  Version.foil,           value == "foil"
    [backends.foil-ministral] →  Version.foil_ministral,  value == "foil-ministral"

Hyphens and dots in TOML keys are replaced with underscores to produce valid Python
identifiers. The original string (e.g. "foil-ministral") is preserved as the enum value
and used in S3 cache paths, log lines, and API responses.

`Version` instances are also `str` (declared with `type=str`), so they can be used
directly wherever a string is expected (e.g. f-strings, dict keys, FastAPI path params).

## Why the local import ?

`BackendConfig` is imported inside `_load()`, not at the top of the file.
loader.py lives inside config/, and a top-level import of backends/ would trigger
backends/__init__.py before config.settings is fully initialized on some import paths.
The local import defers it until after all config submodules are ready.

## What is exported ?

- `Version`         : the dynamic Enum class — used as a FastAPI path parameter type.
- `VERSION_CONFIGS` : maps each Version member to its ProcessingConfig (URL, keys, params…).
"""

import tomllib
from enum import Enum
from pathlib import Path
from typing import Any

from contracts import ProcessingConfig, resolve_env_placeholders  # noqa: F401
from config.settings import settings


def _load() -> tuple[type[Enum], dict[Enum, ProcessingConfig]]:
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

    # Build the enum from backend names: hyphens/dots → underscores for valid identifiers.
    # type=str makes each member a str subclass, so Version.foil == "foil" is True.
    _Version = Enum(
        "Version",
        {name.replace(".", "_").replace("-", "_"): name for name in data.backends},
        type=str,
    )
    _VERSION_CONFIGS: dict[Enum, ProcessingConfig] = {
        _Version(name): cfg for name, cfg in data.backends.items()
    }
    return _Version, _VERSION_CONFIGS


# Module-level annotations: static type checkers cannot infer these from Enum(),
# so we declare them explicitly. The actual types are assigned by _load() below.
Version: type[Enum]
VERSION_CONFIGS: dict[Enum, ProcessingConfig]
Version, VERSION_CONFIGS = _load()