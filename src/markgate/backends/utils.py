"""Shared utilities for backend handlers — no imports from other backend modules."""

import hashlib
import re
from typing import Any

_CLIENT_HEADERS_BLOCKLIST: frozenset[str] = frozenset({"authorization"})

IMAGES_DIR = "imgs"
_CONTENT_HASH = re.compile(r"^[0-9a-f]{16}$")


EMPTY_CONTENT_REASON = "the document has no extractable content"


class UpstreamProcessingError(Exception):
    """The backend answered but couldn't convert the document.

    `details` (the backend's own error payload) is archived with the failed request (see
    services.save_failed_request). `client_reason` is what the client is told (defaults to the
    message), with `status_code`: 422 when the document itself yields nothing, 502 otherwise.
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        status_code: int = 502,
        client_reason: str | None = None,
    ):
        super().__init__(message)
        self.details = details
        self.status_code = status_code
        self.client_reason = client_reason or message


def merge_headers(upstream: dict[str, str], custom: dict[str, str]) -> dict[str, str]:
    """Build the header dict to send to an upstream backend.

    - Client authentication headers (blocklist) are stripped from upstream.
    - custom (from ProcessingConfig) wins on any key conflict.
    """
    filtered = {
        k: v for k, v in upstream.items() if k.lower() not in _CLIENT_HEADERS_BLOCKLIST
    }
    return {**filtered, **custom}


def content_image_name(data: bytes, ext: str) -> str:
    """Name an image after its content: "imgs/<first 16 hex of sha256(data)>.<ext>" (foil-serve's
    convention). Same image, same name: stored once per backend, found by name (POST /images)."""
    return f"{IMAGES_DIR}/{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def normalize_content_image_name(name: str, extensions: tuple[str, ...]) -> str | None:
    """Accept "<sha16>.<ext>" or "imgs/<sha16>.<ext>" with an allowed ext; return
    "imgs/<sha16>.<ext>", or None if `name` isn't a content-named image."""
    basename = name.removeprefix(f"{IMAGES_DIR}/")
    stem, _, ext = basename.partition(".")
    if ext not in extensions or not _CONTENT_HASH.fullmatch(stem):
        return None
    return f"{IMAGES_DIR}/{basename}"
