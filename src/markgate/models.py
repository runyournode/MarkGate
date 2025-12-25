from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from urllib.parse import unquote


class ExternalDocumentRequestHeaders(BaseModel):
    """
    Header received from owui
    """

    content_type: str = Field(alias="Content-Type")  # mime-type
    x_filename: str = Field(alias="X-Filename")  # filename
    model_config = ConfigDict(populate_by_name=True)

    @property
    def filename(self) -> str:
        # clean (human-readable) name from the quoted X-Filename (e.g. %20 -> space)
        return unquote(self.x_filename)


class VersionMetadata(BaseModel):
    """
    Metadata for S3
    """

    version: str
    created_at: datetime
    last_hit_at: datetime
    hit_count: int
    last_filename_used: str


class GlobalFileAliases(BaseModel):
    """
    File aliases for S3
    """

    file_hash: str
    filenames: list[str] = []


class ProcessedDocument(BaseModel):
    """
    Response from processing backend
    """

    page_content: str
    metadata: dict


ExternalDocumentOutput = ProcessedDocument | list[ProcessedDocument] | dict
