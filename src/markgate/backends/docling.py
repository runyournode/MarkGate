"""Backend handler for Docling-serve.

API contract:
  POST {upstream_url}
  Body: multipart/form-data — file in "files" field, query_params as form fields
  Nested dict/list values in query_params are JSON-serialized (docling expects JSON strings).
  Response JSON: {document: {md_content: str}, status: str, processing_time: float, errors: [...]}
"""

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from backends.utils import merge_headers
from contracts import ProcessingConfig
from schemas import Metadata, ProcessedDocument

_IMAGE_DESCRIPTION_DEFAULT_PROMPT = (
    "You are a document analysis expert who specializes in creating text descriptions for images.\n"
    "You will receive an image of a picture or figure. Your job is to create a short description of the image.\n"
    "Instructions:\n"
    "1. Carefully examine the provided image.\n"
    "2. Output a faithful description of the image with enough specific detail to accurately reconstruct it.\n"
    "If the image is a figure or contains alpha-numeric data, include that data in the output."
)


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class PictureDescriptionApiConfig(BaseModel):
    """Config for picture description via an OpenAI-compatible API endpoint."""

    model_config = ConfigDict(extra="forbid")

    url: str
    """OpenAI-compatible chat completion endpoint (e.g. vLLM, LM Studio)."""

    headers: dict[str, str] = {}
    """HTTP headers forwarded to the API (e.g. Authorization: Bearer <token>)."""

    params: dict[str, Any] = {}
    """Model parameters (e.g. model name, max_completion_tokens, temperature)."""

    timeout: float = 20.0
    """Request timeout in seconds."""

    concurrency: int = 1
    """Max concurrent picture-description requests."""

    prompt: str = _IMAGE_DESCRIPTION_DEFAULT_PROMPT
    """System prompt sent with each picture description request."""


class DoclingQueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_formats: list[str] | None = None
    """Input formats to accept. None = all supported formats."""

    to_formats: list[str] = ["md"]
    """Output formats to generate."""

    image_export_mode: Literal["embedded", "referenced", "placeholder"] = "placeholder"

    include_images: bool = True
    """Extract images from the document."""

    do_ocr: bool = True
    force_ocr: bool = False

    ocr_engine: str = "auto"
    """OCR engine: 'auto', 'easyocr', or 'tesseract'."""

    ocr_lang: list[str] | None = None
    """OCR languages (e.g. ['en', 'fr']). None = engine default."""

    pdf_backend: Literal[
        "docling_parse", "pypdfium2", "dlparse_v1", "dlparse_v2", "dlparse_v4"
    ] = "docling_parse"

    do_table_structure: bool = True
    table_mode: Literal["fast", "accurate"] = "fast"
    table_cell_matching: bool = True

    abort_on_error: bool = False

    do_picture_classification: bool = False

    do_picture_description: bool = False
    picture_description_area_threshold: float = 0.0
    """Minimum picture area (fraction of page) to trigger description."""

    picture_description_preset: str | None = None
    """Named preset for picture description (e.g. 'smolvlm', 'granite_vision')."""

    picture_description_api: PictureDescriptionApiConfig | None = None
    """OpenAI-compatible API config for picture description (alternative to preset)."""

    do_formula_enrichment: bool = False
    do_code_enrichment: bool = False

    md_page_break_placeholder: str = ""
    """String inserted between pages in Markdown output."""


class DoclingConfig(ProcessingConfig):
    backend_type: Literal["docling"]
    query_params: DoclingQueryParams = DoclingQueryParams()

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
    merged = merge_headers(headers, config.custom_headers)

    # Content-Type must NOT appear at request level: httpx sets
    # "multipart/form-data; boundary=..." automatically, and an explicit
    # override would break the boundary declaration.
    files = {"files": (filename, file_content, merged["Content-Type"])}
    request_headers = {k: v for k, v in merged.items() if k.lower() != "content-type"}

    # Docling expects nested dicts/lists as JSON strings in form data
    form_data = {
        k: json.dumps(v) if isinstance(v, (dict, list)) else v
        for k, v in config.get_raw_query_params().items()
    }

    resp = await client.post(
        url=config.upstream_url,
        files=files,
        data=form_data,
        headers=request_headers,
    )
    resp.raise_for_status()
    data = resp.json()

    page_content: str = data.get("document", {}).get("md_content", "")

    return ProcessedDocument(
        page_content=page_content,
        metadata=Metadata(
            {
                "status": data.get("status"),
                "processing_time": data.get("processing_time"),
                "errors": data.get("errors"),
            }
        ),
        images={},
    )