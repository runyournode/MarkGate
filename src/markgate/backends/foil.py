"""Backend handler for Foil-serve.

API contract:
  POST {upstream_url}
  Body: raw file bytes
  Params: query_params as URL query parameters
  Response JSON: {page_content: str, images: {name: b64}, metadata: {...}}
"""

import asyncio
import json
from enum import StrEnum
from typing import Any, Literal

import httpx
from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from backends.utils import merge_headers
from contracts import ProcessingConfig
from media import batch_b64_to_pil
from schemas import Metadata, ProcessedDocument

# MIME types for which foil-serve's spreadsheet_mode/excel_min_output_ratio actually have an
# effect (.xls/.xlsx/.ods) — see Foil-Serve CLAUDE.md: "Ignored for non-spreadsheet inputs".
SPREADSHEET_MIME_TYPES = {
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.spreadsheet",
}


# ---------------------------------------------------------------------------
# Per-request overrides
# ---------------------------------------------------------------------------


class SpreadsheetMode(StrEnum):
    """Mirrors foil_serve.schemas.SpreadsheetMode — the values foil-serve accepts."""

    AUTO = "auto"
    PANDAS = "pandas"
    OCR = "ocr"
    BOTH = "both"


class SpreadsheetOverrides(BaseModel):
    """Per-request overrides for foil-backed Versions.

    A field left at None means "no override — use this Version's configured value" (whether that
    value comes from backend_config.toml or from the field's own default).

    extra="forbid" doesn't affect the query-string path (as_query() below only ever constructs
    this from its own declared Query() params) — it matters for FoilRouteAlias.query_params,
    where this model validates a TOML dict: a typo'd/unsupported key there must fail at startup,
    not be silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    spreadsheet_mode: SpreadsheetMode | None = None
    excel_min_output_ratio: float | None = None

    @classmethod
    def as_query(
        cls,
        spreadsheet_mode: SpreadsheetMode | None = Query(
            None,
            description="Override this Version's spreadsheet conversion strategy "
            "(auto/pandas/ocr/both). Ignored for non-spreadsheet inputs.",
        ),
        excel_min_output_ratio: float | None = Query(
            None,
            ge=0.0,
            description="Override the sparse-output threshold that triggers PDF+OCR fallback "
            "in 'auto' mode. Ignored outside 'auto' mode and for non-spreadsheet inputs.",
        ),
    ) -> "SpreadsheetOverrides":
        return cls(
            spreadsheet_mode=spreadsheet_mode,
            excel_min_output_ratio=excel_min_output_ratio,
        )


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class FoilQueryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_description_model_name: str = ""
    """VLM model name used by foil-serve for image description. Empty string = no VLM."""

    spreadsheet_mode: SpreadsheetMode = SpreadsheetMode.AUTO
    """Spreadsheet conversion strategy. Matches foil-serve's own default (auto)."""

    excel_min_output_ratio: float | None = None
    """Sparse-output threshold override for 'auto' mode. None = use foil-serve's configured default."""


class FoilRouteAlias(BaseModel):
    """A static route alias exposing a preset of SpreadsheetOverrides as a URL path segment,
    for clients that cannot set query params.

    Registered under the fixed `/alias` prefix as `/alias/md/{version}/{name}/process`,
    `/alias/{version}/{name}/process` and `/alias/{version}/{name}/process/download` — `{name}`
    always sits between `{version}` and `process` so the path still ends in `/process`(`/download`).
    Each route resolves to the exact same FoilConfig (and therefore the exact same cache entry /
    processing lock, see FoilConfig.cache_key()) as calling the parent Version with the equivalent
    query params.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=r"^[a-z0-9_-]+$",
        min_length=1,
        description="URL path segment, e.g. 'spreadsheet_ocr' for /alias/{version}/spreadsheet_ocr/process.",
    )
    query_params: SpreadsheetOverrides
    """Overrides applied on top of the parent Version's query_params — same shape and same
    validation as the ?spreadsheet_mode=...&excel_min_output_ratio=... query string overrides."""


class FoilConfig(ProcessingConfig):
    backend_type: Literal["foil"]
    query_params: FoilQueryParams = FoilQueryParams()
    aliases: list[FoilRouteAlias] = Field(default_factory=list)

    def with_overrides(self, overrides: SpreadsheetOverrides | None) -> "FoilConfig":
        """Return a copy of this config with `overrides` merged over its query_params.

        Reconstructs FoilQueryParams (rather than model_copy(update=...)) so the merge is
        re-validated — an invalid override fails here, not silently downstream.
        """
        if not overrides:
            return self
        merged = FoilQueryParams(
            **{
                **self.query_params.model_dump(),
                **overrides.model_dump(exclude_none=True),
            }
        )
        return self.model_copy(update={"query_params": merged})

    def get_raw_query_params(self, mime: str = "") -> dict[str, Any]:
        """Resolve this config's query params for a file of the given MIME type.

        Drops keys that are inert for `mime` so that (a) we don't send foil-serve params it will
        ignore anyway and (b) `cache_key()` — built from this same dict — doesn't fragment the
        cache over params that can't have changed the output.
        """
        raw = self.query_params.model_dump(exclude_none=True)

        if mime not in SPREADSHEET_MIME_TYPES:
            raw.pop("spreadsheet_mode", None)
            raw.pop("excel_min_output_ratio", None)
        elif raw.get("spreadsheet_mode") != SpreadsheetMode.AUTO:
            # excel_min_output_ratio only drives the sparse-detection fallback in 'auto' mode
            # (foil_serve.processing.resolve_strategy) — inert for pandas/ocr/both.
            raw.pop("excel_min_output_ratio", None)

        if not raw.get("image_description_model_name"):
            # Passing an empty string here is not compatible with foil-serve — omit the key
            # entirely rather than dropping the whole dict (as the previous implementation did).
            raw.pop("image_description_model_name", None)

        return raw

    def cache_key(self, version: str, mime: str) -> str:
        """Human-readable cache/lock key derived from the resolved params, not the Version name.

        Two FoilConfigs — a preset Version, or the base Version plus a per-request override —
        that resolve to the same params dict produce the same key here, so they share one cache
        entry / one processing lock, regardless of which Version or override produced them.
        """
        params = self.get_raw_query_params(mime)
        if not params:
            return self.backend_type
        slug = ",".join(
            f"{k}={json.dumps(v, sort_keys=True, default=str)}"
            for k, v in sorted(params.items())
        )
        return f"{self.backend_type}/{slug}"


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
    assert isinstance(config, FoilConfig)
    mime = headers.get("Content-Type", "")
    params = config.get_raw_query_params(mime)

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
