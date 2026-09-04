"""Example auto-selector for /auto/* routes (see routing.py / auto_routes.py).

Enable with:
    AUTO_ROUTE_ENABLED=true
    AUTO_SELECTOR_PATH=/path/to/this/file  (or your own copy/variant)

Demo policy — illustrative, tune thresholds and targets to your own backends before relying on
this in production:
    - file <  10 MB -> "foil-ministral-3-3b" (VLM image description)
    - file >= 10 MB -> "foil", spreadsheet_mode=auto, excel_min_output_ratio=0.99

TEMPORARY LOCATION: this file lives under src/ for now. It belongs under
docker/mounts_config/markgate/config/ long-term, alongside backend_config.toml (same bind-mount
pattern) — move it there in the docker mounts pass.

Needs no extra dependency beyond what MarkGate already requires. A selector that does (e.g. a
PDF-parsing library for a page-count-based policy) should declare it in pyproject.toml's
`auto-routing` dependency group rather than MarkGate's core dependencies.
"""

from backends.foil import SpreadsheetMode, SpreadsheetOverrides
from config.loader import Version
from routing import BackendSelection, BackendSelectionContext

SIZE_THRESHOLD_BYTES = 10 * 1024 * 1024


async def select(ctx: BackendSelectionContext) -> BackendSelection:
    if ctx.size_bytes < SIZE_THRESHOLD_BYTES:
        return BackendSelection(version=Version("foil-ministral-3-3b"))
    return BackendSelection(
        version=Version("foil"),
        overrides=SpreadsheetOverrides(
            spreadsheet_mode=SpreadsheetMode.AUTO,
            excel_min_output_ratio=0.99,
        ),
    )
