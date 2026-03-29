"""Backend handler for Foil-serve.

API contract:
  POST {upstream_url}
  Body: raw file bytes
  Params: query_params as URL query parameters
  Response JSON: {page_content: str, images: {name: b64}, metadata: {...}}
"""

import asyncio
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from backends.utils import merge_headers
from contracts import ProcessingConfig
from media import batch_b64_to_pil
from schemas import Metadata, ProcessedDocument


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class FoilQueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_description_model_name: str = ""
    """VLM model name used by foil-serve for image description. Empty string = no VLM."""


class FoilConfig(ProcessingConfig):
    backend_type: Literal["foil"]
    query_params: FoilQueryParams = FoilQueryParams()

    def get_raw_query_params(self) -> dict[str, Any]:
        return self.query_params.model_dump(exclude_none=True)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def call(
    config: ProcessingConfig,
    file_content: bytes,
    headers: dict[str, str],
    filename: str,
    client: httpx.AsyncClient,
) -> ProcessedDocument:

    # Pass {} when no img description (passing empty str param for img_desc is not compatible with foil-serve)
    params = config.get_raw_query_params()
    params = params if params.get('image_description_model_name') else {}

    resp = await client.post(
        url=config.upstream_url,
        content=file_content,
        params=params,
        headers=merge_headers(headers, config.custom_headers),
    )

    resp.raise_for_status()
    data = resp.json()

    page_content: str = data.get("page_content", "")
    if not page_content:
        raise ValueError(f"Upstream returned empty page_content. Full response: {data}")

    imgs = await asyncio.to_thread(batch_b64_to_pil, data.get("images", {}))

    return ProcessedDocument(
        page_content=page_content,
        images=imgs,
        metadata=Metadata(data.get("metadata", {})),
    )