"""Optional auto backend selection: the contract between /auto/* (see auto_routes.py) and a
user-supplied Python file that decides which backend to call for a given upload.

Deliberately minimal — no Protocol, no class registry, no multi-candidate fallback chains. Exactly
one entry point: a file, loaded from AUTO_SELECTOR_PATH, exposing a module-level
`select(ctx: BackendSelectionContext) -> BackendSelection` function. `load_selector()` either finds
that one working function or raises — there is nothing to fall back to.

The output type (`BackendSelection`) is deliberately narrow: a Version plus an optional overrides
object, both fed straight into `VERSION_CONFIGS[version].with_overrides(overrides)` — the exact
merge function client query-param overrides and backend_config.toml aliases already go through. A
selector can therefore pick a different Version outright, or tweak an existing Version's params
(e.g. spreadsheet_mode), but never fabricate an ad-hoc config — which is what keeps the S3
cache-entry / Redis-lock convergence guarantee intact regardless of which route resolved a given
(Version, overrides) pair (see FoilConfig.cache_key() and auto_routes.py's module docstring).
"""

import importlib.util
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from config.settings import settings
from contracts import ProcessingConfig

# Typed against the plain stdlib Enum, not config.loader.Version — same reason
# config/loader.py's _resolve_aliases() does: Version is a dynamically-built subclass, and typing
# against its abstract base keeps this module (and its tests) decoupled from having a real,
# loaded backend_config.toml at import time. See tests/test_route_aliases.py's FakeVersion for the
# established pattern this mirrors.


@dataclass(frozen=True)
class BackendSelectionContext:
    """Everything a selector gets to base its decision on — cheap to know up front, computed once
    per /auto/* request before any selection logic runs."""

    file_content: bytes
    filename: str
    extension: str
    """Lowercased, including the leading dot (e.g. ".pdf"). "" if the filename has none."""
    declared_content_type: str
    """The client's Content-Type header — untrusted, same as everywhere else in this app."""
    sniffed_mime: str
    """media.get_mime_type(file_content) — authoritative, libmagic-detected."""
    size_bytes: int
    available_versions: Mapping[Enum, ProcessingConfig]
    """VERSION_CONFIGS, so a selector can reference real backend names and introspect what's
    actually declared in this deployment's backend_config.toml."""


@dataclass(frozen=True)
class BackendSelection:
    """A selector's decision: which Version to call, and optionally how to override it."""

    version: Enum
    overrides: Any = None
    """Passed to VERSION_CONFIGS[version].with_overrides(overrides) unchanged. Must match the type
    that Version's with_overrides() expects (e.g. backends.foil.SpreadsheetOverrides for a
    foil-family Version). None = use the Version's config as configured, no override."""


class NoSuitableBackendError(Exception):
    """Raise this from select() when a file legitimately can't be auto-routed (e.g. an unsupported
    extension). Maps to HTTP 422 — distinct from an unexpected exception, which propagates as a
    500 (fail fast, not swallowed)."""


SelectFn = Callable[[BackendSelectionContext], Awaitable[BackendSelection]]


def load_selector() -> SelectFn:
    """Load settings.auto_selector_path as a module and return its `select` function.

    Called once, eagerly, by auto_routes.build_router() at startup — fails fast on a missing path,
    an unreadable file, or a file without a callable `select`, rather than on the first request.
    """
    path = settings.auto_selector_path
    if not path:
        raise ValueError(
            "AUTO_ROUTE_ENABLED is true but AUTO_SELECTOR_PATH is not set."
        )
    spec = importlib.util.spec_from_file_location("markgate_auto_selector", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"Cannot load auto selector module from '{path}'.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    select_fn = getattr(module, "select", None)
    if not callable(select_fn):
        raise TypeError(
            f"'{path}' must define a module-level function `select(ctx) -> BackendSelection`."
        )
    return select_fn
