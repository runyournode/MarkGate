"""Backend handler for Chandra — not yet implemented."""

import httpx

from contracts import ProcessingConfig
from schemas import ProcessedDocument


async def call(
    config: ProcessingConfig,
    file_content: bytes,
    headers: dict[str, str],
    filename: str,
    client: httpx.AsyncClient,
) -> ProcessedDocument:
    raise NotImplementedError("Chandra backend handler is not yet implemented")