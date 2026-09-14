"""Media utilities: MIME detection, file extensions and archive building."""

import mimetypes
import tarfile
from io import BytesIO

import magic
import zstandard
from pydantic import BaseModel


def image_mime(name: str) -> str:
    """MIME type of an image from its file name (e.g. "imgs/6f71….jpg" -> "image/jpeg")."""
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


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
