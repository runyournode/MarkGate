"""Generate schemas/backends.schema.json from the BackendConfig Pydantic model.

Usage:
    uv run python scripts/gen_schema.py

The output file is referenced by [tool.tombi] in pyproject.toml for IDE
autocompletion and validation of backend_config.toml files (VSCode + PyCharm).
"""

import json
import sys
from pathlib import Path

# Internal modules use bare imports (e.g. `from backends import ...`), so
# src/markgate must be on sys.path — same as the runtime environment.
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "markgate"))

from backends import BackendConfig  # noqa: E402

OUTPUT = Path(__file__).parent.parent / "schemas" / "backends.schema.json"


def main() -> None:
    schema = BackendConfig.model_json_schema()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(schema, indent=2))
    print(f"Schema written to {OUTPUT}")


if __name__ == "__main__":
    main()