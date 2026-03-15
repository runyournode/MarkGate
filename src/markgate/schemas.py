import base64
import io
from datetime import datetime
from urllib.parse import unquote
from typing import Any

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    RootModel,
    field_validator,
    field_serializer,
)
from PIL import Image


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

    Images are decoded from base64 to PIL inside `call_upstream_backend`.
    Not sent to the client. Not JSON-serializable due to PIL Image objects.
    """

    page_content: str
    images: dict[str, Image.Image] = Field(default_factory=dict)
    metadata: Metadata | None = None

    # allows to have non serializable data in the object (pil images)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # allow model_dump_json (or model_dump(mode='json')
    @field_serializer("images", when_used="json")
    def serialize_images(self, images: dict[str, Image.Image]):
        result = {}

        for key, img in images.items():
            buffer = io.BytesIO()

            # use original format if known, default to PNG
            format_ = img.format or "PNG"
            img.save(buffer, format=format_)

            result[key] = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return result

    # allow loading from a model_dump_json
    @field_validator("images", mode="before")
    @classmethod
    def deserialize_images(cls, value):
        if not value:
            return {}

        result = {}

        for key, img_data in value.items():
            if isinstance(img_data, Image.Image):
                result[key] = img_data
                continue

            img_bytes = base64.b64decode(img_data)
            buffer = io.BytesIO(img_bytes)

            img = Image.open(buffer)
            img.load()  # important
            buffer.close()

            result[key] = img

        return result


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
