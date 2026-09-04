import logging
from typing import Callable, Coroutine

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.loader import Version, VERSION_CONFIGS
from config.settings import settings

logger = logging.getLogger("markgate")

_bearer = HTTPBearer(auto_error=False)


def _check_bearer(version: Version, auth: HTTPAuthorizationCredentials | None) -> str:
    """Check that the client's Bearer token matches the expected key for this version.

    Shared by verify_api_key (base routes, `version` comes from the path param) and
    make_alias_api_key_verifier (alias routes, `version` is fixed at route-registration time —
    there's no `{version}` path segment to read it from, see main.py's _register_alias_routes()).
    """
    api_key = auth.credentials if auth else None
    expected_key = VERSION_CONFIGS[version].authorized_api_key
    if not api_key or api_key != expected_key:
        masked_key = (api_key[:4] + "***") if api_key else "None"
        logger.warning(
            f"AUTH | Unauthorized access for {version.value} | Key: {masked_key}"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Unauthorized access for version {version.value}. Key provided: {masked_key}",
        )
    return api_key


async def verify_api_key(
    version: Version,
    auth: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Check that the client's Bearer token matches the expected key for this version."""
    return _check_bearer(version, auth)


def make_alias_api_key_verifier(
    version: Version,
) -> Callable[..., Coroutine[None, None, str]]:
    """Build a Depends()-compatible verifier for a route alias, whose Version is fixed at
    registration time rather than read from a `{version}` path param. Same check, same expected
    key as the parent Version — an alias never gets its own API key."""

    async def _verify(
        auth: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> str:
        return _check_bearer(version, auth)

    return _verify


async def verify_api_key_auto(
    auth: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Check the client's Bearer token for /auto/* routes.

    Per-version keys (_check_bearer) don't apply here: the version isn't known until after the
    file has been inspected, so there's no single expected key to check against ahead of time.
    Uses a dedicated CLIENT_API_KEY_AUTO instead — fails closed (always rejects) if it's unset,
    same as leaving a backend's authorized_api_key blank would.
    """
    api_key = auth.credentials if auth else None
    expected_key = settings.client_api_key_auto
    if not api_key or not expected_key or api_key != expected_key:
        masked_key = (api_key[:4] + "***") if api_key else "None"
        logger.warning(f"AUTH | Unauthorized access for /auto/* | Key: {masked_key}")
        raise HTTPException(
            status_code=403,
            detail=f"Unauthorized access for /auto/*. Key provided: {masked_key}",
        )
    return api_key
