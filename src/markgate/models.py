from datetime import datetime
from urllib.parse import unquote
from typing import Any
from pydantic import BaseModel, Field, ConfigDict, RootModel


class ExternalDocumentRequestHeaders(BaseModel):
    """
    Header received from the client (open-webui)
    """

    content_type: str = Field(alias="Content-Type")  # mime-type
    x_filename: str = Field(alias="X-Filename")  # filename
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


class ProcessedDocument(BaseModel):
    """
    Response from processing backend
    """

    page_content: str
    metadata: Metadata


# Response of this proxy to the client
ProxyOutput = ProcessedDocument | list[ProcessedDocument] | dict
