"""Backend handler registry and backend_config.toml root schema.

Each backend module exposes:
  - A ``call`` coroutine matching the ``BackendHandler`` protocol
  - A ``*Config`` subclass of ProcessingConfig with typed query_params

To add a new backend: implement ``call`` + ``*Config`` in a new module,
register it in ``BACKEND_HANDLERS``, and add its config class to ``AnyProcessingConfig``.
"""

from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, Field

from backends import chandra, docling, foil, marker
from backends.docling import DoclingConfig
from backends.foil import FoilConfig
from contracts import ProcessingConfig
from schemas import ProcessedDocument


class BackendHandler(Protocol):
    async def __call__(
        self,
        config: ProcessingConfig,
        file_content: bytes,
        headers: dict[str, str],
        filename: str,
        client: httpx.AsyncClient,
    ) -> ProcessedDocument: ...


BACKEND_HANDLERS: dict[str, BackendHandler] = {
    "foil": foil.call,
    "docling": docling.call,
    "marker": marker.call,
    "chandra": chandra.call,
}

AnyProcessingConfig = Annotated[
    FoilConfig | DoclingConfig,
    Field(discriminator="backend_type"),
]


class BackendConfig(BaseModel):
    """Root schema for backend_config.toml."""

    backends: dict[str, AnyProcessingConfig]
