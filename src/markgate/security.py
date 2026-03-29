import logging

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.loader import Version, VERSION_CONFIGS

logger = logging.getLogger("markgate")

_bearer = HTTPBearer(auto_error=False)


async def verify_api_key(
    version: Version,
    auth: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Check that the client's Bearer token matches the expected key for this version."""
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
