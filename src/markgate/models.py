from datetime import datetime
from urllib.parse import unquote
from typing import Any, Annotated

from pydantic import BaseModel, Field, ConfigDict, RootModel, SkipValidation, WithJsonSchema
from PIL import Image


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


# On crée un alias de type pour clarifier le code
# SkipValidation évite que Pydantic essaie de valider l'objet au runtime
# WithJsonSchema empêche le crash de /docs en simulant un type connu
# PillowImage = Annotated[
#     Image.Image,
#     SkipValidation,
#     WithJsonSchema({"type": "string", "description": "PIL Image object (internal use only)"})
# ]


class ResponseDocument(BaseModel):
    """
    Response from this proxy/gateway
    Images are not sent back (yet)
    """
    page_content: str
    metadata: Metadata | None = None

class ProcessedDocument(ResponseDocument):
    """
    Return for `call_upstream_backend` that process the response from the processing backend
    This is an intermediairia/internal format and not sent to the client
    :warning: As we are storing PIL images, it is not serializable !
    """
    page_content: str
    images: dict[str, Image.Image]
    metadata: Metadata | None = None

    # allows to have non serializable data
    model_config = ConfigDict(arbitrary_types_allowed=True)




# Response of this proxy to the client
ProxyOutput = ResponseDocument | list[ResponseDocument] | dict
