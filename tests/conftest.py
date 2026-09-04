"""Pytest-wide setup.

config/loader.py builds Version/VERSION_CONFIGS from backend_config.toml at *module import time*
(not lazily), so any test file that imports config.loader (directly or transitively, e.g. via
security.py or auto_routes.py) needs a resolvable backend_config.toml before that first import
happens. Point BACKEND_CONFIG_PATH at a small fixture file here, at conftest collection time —
before any test module has imported anything from markgate — so it's in place session-wide.

setdefault: a BACKEND_CONFIG_PATH already set in the environment (e.g. to test against a real
config) is left alone.
"""

import os
from pathlib import Path

_FIXTURE_BACKEND_CONFIG = Path(__file__).parent / "fixtures" / "backend_config.toml"
os.environ.setdefault("BACKEND_CONFIG_PATH", str(_FIXTURE_BACKEND_CONFIG))
