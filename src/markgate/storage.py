"""Storage infrastructure: S3 and Redis client lifecycle, and all S3 I/O helpers."""

import logging
from contextlib import asynccontextmanager, AsyncExitStack
from typing import TYPE_CHECKING, TypeVar, Type, Any

import aioboto3
from botocore.exceptions import ClientError
from fastapi import FastAPI
from pydantic import BaseModel
from redis.asyncio import Redis

from config.settings import settings
from media import pil_to_bytes, pil_format_to_mime
from PIL import Image

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client
else:
    S3Client = Any

logger = logging.getLogger("markgate")


# ---------------------------------------------------------------------------
# S3 client manager
# ---------------------------------------------------------------------------


class S3Manager:
    def __init__(self):
        self._client: S3Client | None = None
        self.session = aioboto3.Session()

    @property
    def is_initialized(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> S3Client:
        if self._client is None:
            raise RuntimeError("S3 client accessed before initialization.")
        return self._client

    @client.setter
    def client(self, value: S3Client):
        self._client = value


s3_manager = S3Manager()


def s3_is_active() -> bool:
    """Returns True if S3 cache is enabled and the client is initialized."""
    return settings.s3_cache_enabled and s3_manager.is_initialized


async def check_redis_health() -> tuple[str, str | None]:
    """Probe Redis connectivity. Returns (status, message).
    status: 'ok' | 'unhealthy'
    """
    try:
        ping = redis_manager.client.ping()
        from collections.abc import Awaitable

        assert isinstance(ping, Awaitable), (
            "redis.asyncio.Redis.ping() did not return an awaitable"
        )
        if not await ping:
            raise RuntimeError("Redis ping did not return PONG")
        return "ok", None
    except Exception as e:
        return "unhealthy", str(e)


async def check_s3_health() -> tuple[str, str | None]:
    """Probe S3 connectivity. Returns (status, message).
    status: 'ok' | 'degraded' | 'disabled'
    """
    if not settings.s3_cache_enabled:
        return "disabled", None
    if not s3_manager.is_initialized:
        return "degraded", "S3 client not initialized"
    try:
        await s3_manager.client.head_bucket(Bucket=settings.s3_bucket)
        return "ok", None
    except Exception as e:
        return "degraded", str(e)


# ---------------------------------------------------------------------------
# S3 I/O helpers
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


async def s3_key_exists(key: str) -> bool:
    """Check if a key exists in the S3 bucket."""
    try:
        await s3_manager.client.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise e


async def s3_get_content(key: str) -> str:
    """Retrieve a text object from S3."""
    response = await s3_manager.client.get_object(Bucket=settings.s3_bucket, Key=key)
    async with response["Body"] as stream:
        data = await stream.read()
        return data.decode("utf-8")


async def s3_put_content(key: str, content: str) -> None:
    """Upload a text/markdown object to S3."""
    assert isinstance(content, str)
    await s3_manager.client.put_object(
        Bucket=settings.s3_bucket, Key=key, Body=content, ContentType="text/markdown"
    )


async def s3_put_imgs(root_img_key: str, images: dict[str, Image.Image]) -> None:
    """Upload images to S3, preserving the original format of each image."""
    for name, image in images.items():
        format_ = image.format or "JPEG"
        await s3_manager.client.put_object(
            Bucket=settings.s3_bucket,
            Key=f"{root_img_key}/{name}",
            Body=pil_to_bytes(image),
            ContentType=pil_format_to_mime(format_),
        )


async def s3_get_imgs(root_img_key: str) -> dict[str, bytes]:
    """Retrieve all images stored under root_img_key prefix.
    Returns {relative_path: bytes} where relative_path matches the original image names
    and thus the Markdown image references (e.g. 'imgs/figure_1.jpg').
    """
    prefix = f"{root_img_key}/"
    result: dict[str, bytes] = {}
    response = await s3_manager.client.list_objects_v2(
        Bucket=settings.s3_bucket, Prefix=prefix
    )
    for obj in response.get("Contents", []):
        key: str = obj["Key"]
        relative_path = key[len(prefix) :]
        if not relative_path:
            continue
        img_response = await s3_manager.client.get_object(
            Bucket=settings.s3_bucket, Key=key
        )
        async with img_response["Body"] as stream:
            result[relative_path] = await stream.read()
    return result


async def s3_get_pydantic(key: str, base_model: Type[T]) -> T:
    """Retrieve an S3 object and deserialize it as a Pydantic model."""
    response = await s3_manager.client.get_object(Bucket=settings.s3_bucket, Key=key)
    async with response["Body"] as stream:
        data = await stream.read()
        return base_model.model_validate_json(data)


async def s3_put_pydantic(key: str, model_item: BaseModel) -> None:
    """Upload a Pydantic model as JSON to S3."""
    await s3_manager.client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=model_item.model_dump_json(),
        ContentType="application/json",
    )


# ---------------------------------------------------------------------------
# Redis client manager
# ---------------------------------------------------------------------------


class RedisManager:
    def __init__(self):
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis client is not initialized. Check lifespan.")
        return self._client

    @client.setter
    def client(self, value: Redis):
        self._client = value


redis_manager = RedisManager()


# ---------------------------------------------------------------------------
# App lifespan: initialize and teardown S3 + Redis clients
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start and stop S3 and Redis clients.
    S3 is only initialized when S3_CACHE_ENABLED=True.
    """
    # noinspection PyAbstractClass
    async with AsyncExitStack() as stack:
        redis_c = await stack.enter_async_context(
            Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=0,
                decode_responses=True,
                socket_timeout=settings.redis_socket_timeout,
            )
        )
        redis_manager.client = redis_c

        # Redis startup check — mandatory: app cannot function without it
        redis_status, redis_msg = await check_redis_health()
        if redis_status != "ok":
            raise RuntimeError(f"Redis unreachable at startup: {redis_msg}")
        logger.info(f"Redis OK ({settings.redis_host}:{settings.redis_port})")

        if settings.s3_cache_enabled:
            s3_c = await stack.enter_async_context(
                s3_manager.session.client(
                    service_name="s3",
                    endpoint_url=settings.s3_endpoint,
                    aws_access_key_id=settings.s3_access_key,
                    aws_secret_access_key=settings.s3_secret_key,
                    region_name=settings.s3_region,
                )
            )
            s3_manager.client = s3_c

            # S3 startup check — non-blocking: app runs in cache-bypass mode if unreachable
            s3_status, s3_msg = await check_s3_health()
            if s3_status == "ok":
                logger.info(
                    f"S3 OK ({settings.s3_endpoint}, bucket: {settings.s3_bucket})"
                )
            else:
                logger.warning(
                    f"S3 unreachable at startup ({s3_msg}) — cache bypassed until S3 recovers"
                )
        else:
            logger.info(
                "S3 cache disabled (S3_CACHE_ENABLED=False) — all requests forwarded to upstream"
            )

        yield
