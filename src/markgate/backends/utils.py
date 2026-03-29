"""Shared utilities for backend handlers — no imports from other backend modules."""

_CLIENT_HEADERS_BLOCKLIST: frozenset[str] = frozenset({"authorization"})


def merge_headers(upstream: dict[str, str], custom: dict[str, str]) -> dict[str, str]:
    """Build the header dict to send to an upstream backend.

    - Client authentication headers (blocklist) are stripped from upstream.
    - custom (from ProcessingConfig) wins on any key conflict.
    """
    filtered = {k: v for k, v in upstream.items() if k.lower() not in _CLIENT_HEADERS_BLOCKLIST}
    return {**filtered, **custom}