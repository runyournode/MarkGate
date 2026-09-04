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
- `ALIAS_ROUTES`    : static route aliases (see `backends.foil.FoilRouteAlias`) resolved and
                       validated at import time, ready for main.py to register as routes.
"""

import tomllib
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

from contracts import ProcessingConfig, resolve_env_placeholders  # noqa: F401
from config.settings import settings


class ResolvedAlias(NamedTuple):
    """A backend_config.toml route alias, resolved and validated against its parent Version.

    `config` is the parent Version's ProcessingConfig with the alias's query params already
    merged in via with_overrides() — the exact same resolution a per-request query-param
    override goes through, so an alias route and its query-param equivalent share one
    cache_key() (see FoilConfig.cache_key()).

    `overrides` is the alias's declared query params as-is (e.g. a SpreadsheetOverrides
    instance) — kept alongside the merged `config` so main.py can spell out, in each alias
    route's OpenAPI description, the exact query-param URL it's equivalent to.
    """

    version: Enum
    name: str
    config: ProcessingConfig
    overrides: Any


def _resolve_aliases(
    version_configs: dict[Enum, ProcessingConfig],
) -> list[ResolvedAlias]:
    """Validate every backend's declared route aliases and resolve each to its effective config.

    A backend_type that doesn't support overrides (e.g. docling) can't declare `aliases` at all —
    its ProcessingConfig subclass simply has no such field, and `extra="forbid"` on that model
    rejects `[backends.X.aliases]` in the TOML at the BackendConfig validation step, above. What's
    left to check here is specific to *resolving* declared aliases: unique names per backend, and
    that the declared query_params actually validate once merged onto the parent config — reusing
    with_overrides() means this is the exact same validation a request-time override goes through.
    """
    resolved: list[ResolvedAlias] = []
    for version, cfg in version_configs.items():
        seen_names: set[str] = set()
        for alias in getattr(cfg, "aliases", []):
            if alias.name in seen_names:
                raise ValueError(
                    f"Duplicate route alias name '{alias.name}' under backend "
                    f"'{version.value}' in backend_config.toml — alias names must be "
                    "unique per backend."
                )
            seen_names.add(alias.name)
            resolved_config = cfg.with_overrides(alias.query_params)
            resolved.append(
                ResolvedAlias(
                    version=version,
                    name=alias.name,
                    config=resolved_config,
                    overrides=alias.query_params,
                )
            )
    return resolved


def _load() -> tuple[type[Enum], dict[Enum, ProcessingConfig], list[ResolvedAlias]]:
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
    _ALIAS_ROUTES = _resolve_aliases(_VERSION_CONFIGS)
    return _Version, _VERSION_CONFIGS, _ALIAS_ROUTES


# Module-level annotations: static type checkers cannot infer these from Enum(),
# so we declare them explicitly. The actual types are assigned by _load() below.
Version: type[Enum]
VERSION_CONFIGS: dict[Enum, ProcessingConfig]
ALIAS_ROUTES: list[ResolvedAlias]
Version, VERSION_CONFIGS, ALIAS_ROUTES = _load()
