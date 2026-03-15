"""Media utilities: PIL image serialization/deserialization and archive building."""

import base64
import mimetypes
import tarfile
from io import BytesIO

import magic
import zstandard
from PIL import Image
from pydantic import BaseModel


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


def pil_format_to_mime(format_: str) -> str:
    """Resolve a PIL format string to a MIME type.
    Fast-path via known dict, then falls back to PIL's own extension registry + stdlib mimetypes.
    """
    if mime := _PIL_FORMAT_TO_MIME.get(format_):
        return mime
    if (ext := _PIL_FORMAT_TO_EXT.get(format_)) and (
        mime := mimetypes.types_map.get(ext.lower())
    ):
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


def batch_pil_to_bytes(images: dict[str, Image.Image]) -> dict[str, bytes]:
    """Serialize a dict of PIL images to bytes, preserving original formats."""
    return {name: pil_to_bytes(img) for name, img in images.items()}


def base64_to_pil(base64_str: str) -> Image.Image:
    """Decode a base64 string to a PIL Image."""
    img_data = base64.b64decode(base64_str)
    return Image.open(BytesIO(img_data))


def batch_b64_to_pil(images_b64: dict[str, str]) -> dict[str, Image.Image]:
    """Decode a dict of base64 strings to PIL images."""
    return {name: base64_to_pil(b64) for name, b64 in images_b64.items()}


def get_mime_type(content: bytes) -> str:
    """Detect the MIME type of raw bytes using libmagic."""
    return magic.Magic(mime=True).from_buffer(content)


# Patches for types where mimetypes.guess_extension returns a wrong or misleading extension.
_MIME_TO_EXT_PATCHES: dict[str, str] = {
    "application/xml": ".xml",  # stdlib returns ".xsl"
}


def mime_to_ext(mime: str) -> str:
    """Return the canonical file extension for a MIME type, including the dot.

    Uses mimetypes.guess_extension (stdlib) as the primary source, patched by a
    small override table for known wrong mappings. Falls back to '.bin'.
    """
    if ext := _MIME_TO_EXT_PATCHES.get(mime):
        return ext
    return mimetypes.guess_extension(mime) or ".bin"


def build_tar_zst(
    page_content: str,
    images: dict[str, bytes],
    metadata: BaseModel | None,
    images_error: str | None = None,
) -> bytes:
    """Build an in-memory tar.zst archive.

    Archive layout (image paths are kept as-is to match Markdown references):
        content.md
        metadata.json
        {img_name}          # e.g. imgs/figure_1.jpg
        images_error.txt    # only present when image retrieval failed
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
        if images_error is not None:
            _add(tar, "images_error.txt", images_error.encode("utf-8"))
    tar_buf.seek(0)
    return zstandard.ZstdCompressor().compress(tar_buf.read())
