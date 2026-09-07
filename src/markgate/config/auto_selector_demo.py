"""Example auto-selector for /auto/* routes (see routing.py / auto_routes.py).

Enable with:
    AUTO_ROUTE_ENABLED=true
    AUTO_SELECTOR_PATH=/path/to/this/file  (or your own copy/variant)

Demo policy — illustrative, tune thresholds and targets to your own backends before relying on
this in production:
    - file <  10 MB -> "foil-ministral-3-3b" (VLM image description)
    - file <  15 MB -> "foil", spreadsheet_mode=auto, excel_min_output_ratio=0.99
    - file >= 15 MB -> "xberg"

Needs no extra dependency beyond what MarkGate already requires. A selector that does (e.g. a
PDF-parsing library for a page-count-based policy) should declare it in pyproject.toml's
`auto-routing` dependency group rather than MarkGate's core dependencies.
"""

from backends.foil import SpreadsheetMode, SpreadsheetOverrides
from config.loader import Version
from routing import BackendSelection, BackendSelectionContext

FOIL_VLM_THRESHOLD = 10  # MB
XBERG_THRESHOLD = 15  # MB


async def select(ctx: BackendSelectionContext) -> BackendSelection:
    size_mb = ctx.size_bytes / 1024**2
    if size_mb < FOIL_VLM_THRESHOLD:
        return BackendSelection(version=Version("foil-ministral-3-3b"))
    elif size_mb < XBERG_THRESHOLD:
        return BackendSelection(
            version=Version("foil"),
            overrides=SpreadsheetOverrides(
                spreadsheet_mode=SpreadsheetMode.AUTO,
                excel_min_output_ratio=0.99,
            ),
        )
    else:
        return BackendSelection(version=Version("xberg"))

