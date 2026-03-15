import base64
import mimetypes
import tarfile
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, Type, Any
from io import BytesIO

import zstandard

import magic
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import aioboto3
from botocore.exceptions import ClientError
from redis.asyncio import Redis
from PIL import Image

# enable type-checking without needing dev dependencies at runtime
if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client
else:
    S3Client = Any

from config import settings, Version, VERSION_CONFIGS


# -----------------------------------
# S3 Connection                  -
# -----------------------------------
class S3Manager:
    def __init__(self):
        self.client: S3Client | None = None
        self.session = aioboto3.Session()

    @property
    def client(self) -> S3Client:
        if self._client is None:
            raise RuntimeError("S3 client accessed before initialization.")
        return self._client

    @client.setter
    def client(self, value: S3Client):
        self._client = value


s3_manager = S3Manager()


# -----------------------------------
# I/O on S3 bucket                  -
# -----------------------------------


async def s3_key_exists(key: str) -> bool:
    """
    Check if a key exists in S3 bucket.
    """
    try:
        await s3_manager.client.head_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise e  # Propagate any other error code


async def s3_get_content(key: str) -> str:
    """Récupère un objet S3 de type texte"""
    response = await s3_manager.client.get_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
    )
    async with response["Body"] as stream:
        data = await stream.read()
        return data.decode("utf-8")


async def s3_put_content(key: str, content: str):
    """Envoie un objet S3 de type texte/markdown"""
    assert isinstance(content, str)
    await s3_manager.client.put_object(
        Bucket=settings.S3_BUCKET, Key=key, Body=content, ContentType="text/markdown"
    )


# Fast-path for the most common formats
_PIL_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
}

# Inverted PIL extension registry: {"JPEG": ".jpg", "PNG": ".png", ...}
# Built once at import time. PIL maps extensions → formats; we need the reverse.
_PIL_FORMAT_TO_EXT: dict[str, str] = {
    fmt: ext for ext, fmt in Image.registered_extensions().items()
}


def _pil_format_to_mime(format_: str) -> str:
    """Resolve a PIL format string to a MIME type.
    Fast-path via known dict, then falls back to PIL's own extension registry + stdlib mimetypes.
    """
    if mime := _PIL_FORMAT_TO_MIME.get(format_):
        return mime
    if (ext := _PIL_FORMAT_TO_EXT.get(format_)) and (mime := mimetypes.types_map.get(ext.lower())):
        return mime
    return "application/octet-stream"


def pil_to_bytes(img: Image.Image) -> bytes:
    """Serialize a PIL image to bytes preserving its original format.
    Falls back to JPEG if the format is unknown.
    JPEG-specific options (quality, subsampling) are only applied for JPEG.
    """
    format_ = img.format or "JPEG"
    buffer = BytesIO()
    if format_ == "JPEG":
        img.save(buffer, format=format_, quality=95, subsampling=0)
    else:
        img.save(buffer, format=format_)
    return buffer.getvalue()


async def s3_put_imgs(root_img_key: str, images: dict[str, Image.Image]):
    """Envoie les images dans le S3 en préservant le format original du backend processor."""
    for name, image in images.items():
        format_ = image.format or "JPEG"  # shared source of truth for both Body and ContentType
        await s3_manager.client.put_object(
            Bucket=settings.S3_BUCKET,
            Key=f"{root_img_key}/{name}",
            Body=pil_to_bytes(image),
            ContentType=_pil_format_to_mime(format_),
        )

async def s3_get_imgs(root_img_key: str) -> dict[str, bytes]:
    """Retrieve all images stored under root_img_key prefix.
    Returns {relative_path: bytes} where relative_path matches the original image names
    and thus the Markdown image references (e.g. 'imgs/figure_1.jpg').
    """
    prefix = f"{root_img_key}/"
    result: dict[str, bytes] = {}
    response = await s3_manager.client.list_objects_v2(Bucket=settings.S3_BUCKET, Prefix=prefix)
    for obj in response.get("Contents", []):
        key: str = obj["Key"]
        relative_path = key[len(prefix):]
        if not relative_path:
            continue
        img_response = await s3_manager.client.get_object(Bucket=settings.S3_BUCKET, Key=key)
        async with img_response["Body"] as stream:
            result[relative_path] = await stream.read()
    return result


def build_tar_zst(
    page_content: str,
    images: dict[str, bytes],
    metadata: BaseModel | None,
) -> bytes:
    """Build an in-memory tar.zst archive.

    Archive layout (image paths are kept as-is to match Markdown references):
        content.md
        metadata.json
        {img_name}     # e.g. imgs/figure_1.jpg
    """
    def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        archive.addfile(info, BytesIO(data))

    tar_buf = BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        _add(tar, "content.md", page_content.encode("utf-8"))
        if metadata is not None:
            _add(tar, "metadata.json", metadata.model_dump_json().encode("utf-8"))
        for img_name, img_bytes in images.items():
            _add(tar, img_name, img_bytes)
    tar_buf.seek(0)
    return zstandard.ZstdCompressor().compress(tar_buf.read())



T = TypeVar("T", bound=BaseModel)

async def s3_get_pydantic(key: str, base_model: Type[T]) -> T:
    """Récupère un objet S3 et le transforme directement en modèle Pydantic."""
    response = await s3_manager.client.get_object(Bucket=settings.S3_BUCKET, Key=key)
    async with response["Body"] as stream:
        data = await stream.read()
        return base_model.model_validate_json(data)


async def s3_put_pydantic(key: str, model_item: BaseModel):
    """Envoie un modèle Pydantic sur S3 en format JSON."""
    await s3_manager.client.put_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=model_item.model_dump_json(),
        ContentType="application/json",
    )




# -----------------------------------
# Redis Connection                  -
# -----------------------------------
class RedisManager:
    def __init__(self):
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        """Garantit que le client est initialisé avant utilisation."""
        if self._client is None:
            raise RuntimeError("Redis client is not initialized. Check lifespan.")
        return self._client

    @client.setter
    def client(self, value: Redis):
        self._client = value


# Une seule instance partagée pour tout le module
redis_manager = RedisManager()


# -----------------------------------
# Lifespan management               -
# -----------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Start and stop S3 and Redis clients
    :param _app:
    :return:
    """
    async with (
        s3_manager.session.client(
            service_name="s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
        ) as s3_c,
        Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=0,
            decode_responses=True,  # Très pratique pour avoir des str au lieu de bytes
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
        ) as redis_c,
    ):
        s3_manager.client = s3_c
        redis_manager.client = redis_c
        yield  # L'application tourne ici



# -----------------------------------
# Incoming API key verification     -
# -----------------------------------
# Use HTTPBearer for Authorization header
security = HTTPBearer(auto_error=False)


async def verify_api_key(
    version: Version,
    auth: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """
    Check if processing version is supported and if client has a proper API key.
    """
    api_key = auth.credentials if auth else None
    expected_key = VERSION_CONFIGS[version].authorized_api_key
    if not api_key or api_key != expected_key:
        masked_key = (api_key[:4] + "***") if api_key else "None"
        raise HTTPException(
            status_code=403,
            detail=f"Unauthorized access for version {version.value}. Key provided: {masked_key}",
        )
    return api_key


# -----------------------------------
# Lifespan management               -
# -----------------------------------

def get_mime_type(content: bytes) -> str:
    # Initialise magic pour retourner le type MIME
    mime = magic.Magic(mime=True)
    return mime.from_buffer(content)


def base64_to_pil(base64_str: str) -> Image.Image:
    img_data = base64.b64decode(base64_str)
    img = Image.open(BytesIO(img_data))
    return img