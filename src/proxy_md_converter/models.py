from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

class ExternalDocumentRequestHeaders(BaseModel):
    content_type: str = Field(alias="content-type")
    x_filename: str = Field(alias="x-filename")
    authorization: str | None = None
    model_config = ConfigDict(populate_by_name=True)

class VersionMetadata(BaseModel):
    version: str
    created_at: datetime
    last_hit_at: datetime
    hit_count: int
    last_filename_used: str 

class GlobalFileAliases(BaseModel):
    file_hash: str
    filenames: list[str] = []

class ProcessedDocument(BaseModel):
    page_content: str
    # metadata: ... (simplifié pour l'exemple)

ExternalDocumentOutput = ProcessedDocument | list[ProcessedDocument] | dict
