"""Backend handler for Docling-serve (v1.37, preset-based options).

API contract (asynchronous — the synchronous endpoint gives up after docling-serve's max_sync_wait,
120 s by default, too short for long scans):
  POST {upstream_url}/v1/convert/file/async
       Body: multipart/form-data — file in "files", query_params as form fields
       (list values as repeated fields)
       Response JSON: {task_id: str, task_status: str, ...}
  GET  {upstream_url}/v1/status/poll/{task_id}  every poll_interval_s, until task_status is
       success / partial_success / failure (no `wait` param: docling-serve answers immediately)
  GET  {upstream_url}/v1/result/{task_id}
       Response JSON: {document: {md_content: str | null}, status: str, processing_time: float,
       errors: [{module_name, error_message, ...}]}
       The task can succeed while the document failed (status "failure", md_content null): raised
       as an UpstreamProcessingError carrying docling's errors (502); no Markdown without failure
       is an empty document (422).

upstream_url is the docling-serve base URL (e.g. http://docling-serve:5001).

Images: with image_export_mode = "embedded" (and include_images = true), docling-serve inlines
each picture as a data URI, ![Image](data:image/png;base64,...). They are extracted, named after
their content like foil's ("imgs/<sha16>.png", see backends.utils.content_image_name) and the
Markdown points to that name: same storage, serving and POST /images lookup as foil.
"""

import asyncio
import base64
import re
import time
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from backends.utils import (
    EMPTY_CONTENT_REASON,
    UpstreamProcessingError,
    content_image_name,
    merge_headers,
    normalize_content_image_name,
)
from contracts import ProcessingConfig
from media import mime_to_ext
from schemas import Metadata, ProcessedDocument

_TERMINAL_STATUSES = {"success", "partial_success", "failure"}
_EMBEDDED_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:(?P<mime>image/[\w.+-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)\)"
)
_IMAGE_EXTENSIONS = ("png", "jpg")


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class DoclingQueryParams(BaseModel):
    """docling-serve conversion options. Every field defaults to None and is only sent when set
    in backend_config.toml: the request carries exactly what the config declares, docling-serve's
    own defaults apply to the rest. Presets must exist on the docling-serve side (its config)."""

    model_config = ConfigDict(extra="forbid")

    to_formats: list[str] | None = None
    """Output formats to generate (e.g. ["md"])."""

    image_export_mode: Literal["embedded", "referenced", "placeholder"] | None = None
    """"embedded" to get the pictures (extracted and served like foil's), "placeholder" for none."""
    include_images: bool | None = None
    """Generate picture images (needed with image_export_mode = "embedded")."""
    images_scale: float | None = None
    """Picture image resolution factor (docling-serve's default: 2.0)."""
    from_formats: list[str] | None = None
    """Input formats to accept. Unset = all supported formats."""

    pipeline: Literal["standard", "vlm"] | None = None
    pdf_backend: Literal["docling_parse", "pypdfium2"] | None = None

    # standard pipeline
    layout_preset: str | None = None
    do_ocr: bool | None = None
    force_ocr: bool | None = None
    ocr_preset: str | None = None
    do_table_structure: bool | None = None
    table_structure_preset: str | None = None
    do_formula_enrichment: bool | None = None
    do_code_enrichment: bool | None = None
    code_formula_preset: str | None = None

    # vlm pipeline
    vlm_pipeline_preset: str | None = None
    """Always set it with pipeline = "vlm": without it, docling-serve falls back to its legacy
    model (granite-docling) instead of its default preset."""

    # pictures (both pipelines)
    do_picture_description: bool | None = None
    picture_description_preset: str | None = None
    picture_description_area_threshold: float | None = None
    """Minimum picture area (fraction of page) to trigger description."""
    do_picture_classification: bool | None = None
    picture_classification_preset: str | None = None

    abort_on_error: bool | None = None
    md_page_break_placeholder: str | None = None
    """String inserted between pages in Markdown output."""


class DoclingConfig(ProcessingConfig):
    content_addressed_images: ClassVar[bool] = True

    backend_type: Literal["docling"]
    query_params: DoclingQueryParams = DoclingQueryParams()

    poll_interval_s: float
    """Delay between two task status polls."""

    max_wait_s: float
    """Give up (TimeoutError) if the task isn't finished after this long."""

    @classmethod
    def normalize_image_name(cls, name: str) -> str | None:
        """Accept "<sha16>.png" or "imgs/<sha16>.png" (or .jpg); return "imgs/<sha16>.<ext>"."""
        return normalize_content_image_name(name, _IMAGE_EXTENSIONS)

    def get_raw_query_params(self) -> dict[str, Any]:
        return self.query_params.model_dump(exclude_none=True)


def extract_embedded_images(md: str) -> tuple[str, dict[str, bytes]]:
    """Replace each data-URI image of `md` by its content-based name; return (md, images)."""
    images: dict[str, bytes] = {}

    def to_name(match: re.Match[str]) -> str:
        data = base64.b64decode(match["b64"])
        name = content_image_name(data, mime_to_ext(match["mime"]).lstrip("."))
        images[name] = data
        return f"![{match['alt']}]({name})"

    return _EMBEDDED_IMAGE.sub(to_name, md), images


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
    assert isinstance(config, DoclingConfig)
    merged = merge_headers(headers, config.custom_headers)
    base_url = config.upstream_url.rstrip("/")

    # Content-Type must NOT appear at request level: httpx sets
    # "multipart/form-data; boundary=..." automatically, and an explicit
    # override would break the boundary declaration.
    files = {"files": (filename, file_content, merged["Content-Type"])}
    request_headers = {k: v for k, v in merged.items() if k.lower() != "content-type"}

    # Form fields: lists are sent as repeated fields (to_formats=md&to_formats=json), as docling
    # expects — not as a JSON string; httpx encodes bools as "true"/"false".
    resp = await client.post(
        url=f"{base_url}/v1/convert/file/async",
        files=files,
        data=config.get_raw_query_params(),
        headers=request_headers,
    )
    resp.raise_for_status()
    task_id: str = resp.json()["task_id"]

    deadline = time.monotonic() + config.max_wait_s
    while True:
        resp = await client.get(
            f"{base_url}/v1/status/poll/{task_id}", headers=request_headers
        )
        resp.raise_for_status()
        status = resp.json()
        if status.get("task_status") in _TERMINAL_STATUSES:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"docling task {task_id} not finished after {config.max_wait_s:.0f} s "
                f"(status: {status.get('task_status')})"
            )
        await asyncio.sleep(config.poll_interval_s)

    if status["task_status"] == "failure":
        raise UpstreamProcessingError(
            f"docling task {task_id} failed: {status}",
            details=status,
            client_reason="docling conversion task failed",
        )

    resp = await client.get(f"{base_url}/v1/result/{task_id}", headers=request_headers)
    resp.raise_for_status()
    data = resp.json()

    md_content = (data.get("document") or {}).get("md_content")
    errors = data.get("errors") or []
    summary = "; ".join(
        f"[{e.get('module_name')}] {e.get('error_message')}" for e in errors
    )
    details = {
        "task_id": task_id,
        "status": data.get("status"),
        "errors": errors,
        "processing_time": data.get("processing_time"),
    }
    if data.get("status") == "failure":
        raise UpstreamProcessingError(
            f"docling conversion failed: {summary or 'no error given'}",
            details=details,
            client_reason=summary or "docling conversion failed",
        )
    if not md_content:
        reason = (
            f"{EMPTY_CONTENT_REASON} ({summary})" if summary else EMPTY_CONTENT_REASON
        )
        raise UpstreamProcessingError(
            f"docling returned no Markdown (status: {data.get('status')})",
            details=details,
            status_code=422,
            client_reason=reason,
        )

    page_content, images = extract_embedded_images(md_content)

    return ProcessedDocument(
        page_content=page_content,
        metadata=Metadata(
            {
                "status": data.get("status"),
                "processing_time": data.get("processing_time"),
                "errors": data.get("errors"),
            }
        ),
        images=images,
    )
