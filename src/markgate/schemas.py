import base64
from datetime import datetime
from urllib.parse import unquote
from typing import Any

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    RootModel,
    field_serializer,
)


class ExternalDocumentRequestHeaders(BaseModel):
    """
    Header received from the client (open-webui)
    """

    content_type: str = Field(
        alias="Content-Type",
        description="File MIME type (ex: application/pdf) or application/octet-stream",
        examples=["application/octet-stream", "application/pdf"],
    )
    x_filename: str = Field(
        alias="X-Filename",
        description="Original file name, **URL-encoded (quoted)** (ex: my%20document.pdf)",
        examples=["my%20document.pdf", "%C3%A9tude%202024.pdf"],
    )

    model_config = ConfigDict(populate_by_name=True)

    @property
    def filename(self) -> str:
        # clean (human-readable) name from the quoted X-Filename (e.g. %20 -> space)
        return unquote(self.x_filename)


class S3Metadata(BaseModel):
    """
    Metadata for S3 (_metadata.json)
    """

    version: str
    created_at: datetime
    last_hit_at: datetime
    hit_count: int
    last_filename_used: str


class S3FileAliases(BaseModel):
    """
    File aliases for S3 (_aliases.json)
    """

    file_hash: str
    filenames: list[str] = []


class Metadata(RootModel[dict[str, Any]]):
    """
    Extracted metadata from the backend processor
    """

    root: dict[str, Any] = {}


class ResponseDocument(BaseModel):
    """
    Response from this proxy/gateway
    Images are not sent back (yet)
    """

    page_content: str
    metadata: Metadata | None = None


class ProcessedDocument(ResponseDocument):
    """Internal representation returned by `call_upstream_backend`.

    Images are the raw bytes sent by the backend, keyed by their path in the Markdown
    (e.g. "imgs/6f715daebc26828f.jpg") — never re-encoded.
    """

    page_content: str
    images: dict[str, bytes] = Field(default_factory=dict)
    metadata: Metadata | None = None

    @field_serializer("images", when_used="json")
    def serialize_images(self, images: dict[str, bytes]) -> dict[str, str]:
        return {
            name: base64.b64encode(data).decode("ascii")
            for name, data in images.items()
        }


class ProcessedDocumentOut(ResponseDocument):
    """Public counterpart of ProcessedDocument for routes that include images. Build it from a
    ProcessedDocument via `ProcessedDocumentOut(**doc.model_dump(mode="json"))`."""

    images: dict[str, str] = Field(default_factory=dict)
    """Images as base64-encoded strings, keyed by filename — same encoding as Foil-Serve's own
    /v1/process response."""


class ImagesRequest(BaseModel):
    """Body of POST /images: images of an already processed document, looked up by name."""

    model_config = ConfigDict(extra="forbid")

    engine: str = Field(
        description="Backend type that produced the images — the `extraction_engine` response metadata.",
        examples=["foil"],
    )
    source_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the source document — the `source_sha256` response metadata.",
    )
    names: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Image names as written in the Markdown; the images folder prefix is optional.",
        examples=[["imgs/6f715daebc26828f.jpg", "0a1b2c3d4e5f6a7b.jpg"]],
    )


class ImagePayload(BaseModel):
    mime: str
    data: str
    """base64-encoded image bytes"""


class ImagesResponse(BaseModel):
    images: dict[str, ImagePayload] = Field(default_factory=dict)
    """Found images, keyed by their name as written in the Markdown."""
    missing: list[str] = Field(default_factory=list)
    """Requested names not found in storage."""


class FailedRequestInfo(BaseModel):
    """Saved to S3 under failed_requests/ when an upstream call fails."""

    timestamp: datetime
    version: str
    filename: str
    file_hash: str
    error_message: str
    upstream_duration_ms: float


class ServiceHealth(BaseModel):
    status: str  # "ok" | "degraded" | "unhealthy" | "disabled" | "configured"
    message: str | None = None


class DependenciesHealth(BaseModel):
    redis: ServiceHealth
    s3: ServiceHealth
    backends: dict[str, ServiceHealth]


# Response of this proxy to the client
ProxyOutput = ResponseDocument | list[ResponseDocument] | dict
