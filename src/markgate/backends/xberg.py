"""Backend handler for xberg (locally-run document-extraction server).

API contract:
  POST {upstream_url}   (xberg's native "/extract" endpoint)
  Body: multipart/form-data — file in "files" field, optional "config" field
        (JSON-encoded string; see XbergQueryParams for why extra keys are allowed).
  MarkGate always sends exactly one file and forces output_format="markdown"
  Image extraction is not implemented (yet)
"""

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from backends.utils import merge_headers
from contracts import ProcessingConfig
from schemas import Metadata, ProcessedDocument


class XbergQueryParams(BaseModel):
    # xberg's ExtractionConfig isn't published in its OpenAPI (parsed from a raw multipart JSON
    # string), so MarkGate doesn't model it in full — only the params commonly tweaked in
    # practice are typed below, giving backend_config.toml IDE autocomplete/validation
    # (schemas/backends.schema.json, see scripts/gen_schema.py) and catching typos at startup.
    # `None` = not sent, xberg's own default applies (see get_raw_query_params's exclude_none).
    # output_format always has a value since MarkGate itself depends on it.
    #
    # extra="allow" lets any other ExtractionConfig key reach xberg unvalidated, e.g.:
    # enable_quality_processing, force_ocr_pages, chunking, content_filter, images, pdf_options,
    # token_reduction, language_detection, pages, keywords, postprocessor, html_options,
    # html_output, max_concurrent_extractions, security_limits, max_embedded_file_bytes,
    # jupyter_cell_rendering, acceleration, cache_namespace, cache_ttl_secs, email, concurrency,
    # url, max_archive_depth, structured_extraction, ner, redaction, summarization, translation,
    # page_classification, chunk_classification, captioning, qr_codes.
    model_config = ConfigDict(extra="allow")

    output_format: Literal["markdown", "plain"] = "markdown"
    use_cache: bool | None = None
    force_ocr: bool | None = None
    ocr: bool | None = None
    disable_ocr: bool | None = None
    ocr_strategy: str | None = None
    escape_markdown: bool | None = None
    table_anchors: bool | None = None
    use_layout_for_markdown: bool | None = None
    include_document_structure: bool | None = None
    result_format: str | None = None
    extraction_timeout_secs: int | None = None


class XbergConfig(ProcessingConfig):
    backend_type: Literal["xberg"]
    query_params: XbergQueryParams = XbergQueryParams()

    def get_raw_query_params(self) -> dict[str, Any]:
        return self.query_params.model_dump(exclude_none=True)


def _simplify_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten xberg's metadata down to the handful of fields worth keeping — this ends up
    ingested by open-webui and shown straight to the LLM, so xberg's page dimensions,
    encryption flag, internal source_uri/source_kind bookkeeping etc. don't belong here."""
    fmt = raw.get("format", {})
    additional = raw.get("additional", {})
    simplified = {
        "created_at": raw.get("created_at"),
        "modified_at": raw.get("modified_at"),
        "page_count": fmt.get("page_count"),
        "scanned_pages": fmt.get("scanned_pages"),
        "extraction_duration_ms": raw.get("extraction_duration_ms"),
        "ocr_used": raw.get("ocr_used"),
        "extraction_method": additional.get("extraction_method"),
    }
    return {k: v for k, v in simplified.items() if v is not None}


async def call(
    config: ProcessingConfig,
    file_content: bytes,
    headers: dict[str, str],
    filename: str,
    client: httpx.AsyncClient,
) -> ProcessedDocument:
    assert isinstance(config, XbergConfig)
    merged = merge_headers(headers, config.custom_headers)

    # Content-Type must NOT appear at request level: httpx sets its own
    # "multipart/form-data; boundary=..." — an explicit override breaks the boundary.
    files = {"files": (filename, file_content, merged["Content-Type"])}
    request_headers = {k: v for k, v in merged.items() if k.lower() != "content-type"}
    form_data = {"config": json.dumps(config.get_raw_query_params())}

    resp = await client.post(
        url=config.upstream_url,
        files=files,
        data=form_data,
        headers=request_headers,
    )
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", [])
    if not results:
        raise ValueError(
            f"xberg returned no results (errors: {data.get('errors')}). Full response: {data}"
        )
    document = results[0]
    page_content: str = document.get("content", "")
    if not page_content:
        raise ValueError(f"Upstream returned empty content. Full response: {data}")

    return ProcessedDocument(
        page_content=page_content,
        images={},  # not wired yet
        metadata=Metadata(_simplify_metadata(document.get("metadata", {}))),
    )
