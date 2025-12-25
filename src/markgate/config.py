from typing import Optional, Literal
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    GLOBAL CONFIG FOR THE PROXY
    """

    LOG_LEVEL: str = "INFO"
    LOG_FILE: Optional[str] = None
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB default
    LOG_BACKUP_COUNT: int = 5

    S3_ENDPOINT: str = "http://localhost:3900"
    S3_ACCESS_KEY: str = "admin"
    S3_SECRET_KEY: str = "password"
    S3_BUCKET: str = "doc-cache"

    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # --- INCOMING AUTHENTICATION (Client -> Proxy) ---
    # Keys that clients (e.g. Open WebUI) must provide to use this proxy
    CLIENT_API_KEY_V1: str = "client-secret-v1"
    CLIENT_API_KEY_V2: str = "client-secret-v2"
    CLIENT_API_KEY_V3: str = "client-secret-v3"

    # --- UPSTREAM CONFIGURATION (Proxy -> Backend) ---

    # V1: Marker without llm
    UPSTREAM_V1_URL: str = "http://localhost:9000/process"
    UPSTREAM_V1_API_KEY: str = ""  # Key for the V1 backend

    # V2: Marker with qwen3-vl and image description
    UPSTREAM_V2_URL: str = "http://localhost:9001/process"
    UPSTREAM_V2_API_KEY: str = ""  # Key for the V2 backend
    UPSTREAM_V2_VLLM_URL: str = ""
    UPSTREAM_V2_VLLM_API_KEY: str = ""  # Key for the LLM service used by V2

    # V3: Chandra
    UPSTREAM_V3_URL: str = "http://localhost:9002/process"
    UPSTREAM_V3_API_KEY: str = ""  # Key for the LLM service used by V3

    model_config = SettingsConfigDict(
        env_file=(".env", ".env_secret"),
        env_file_encoding="utf-8",
    )


class ProcessingConfig(BaseModel):
    """
    Configuration of the processing backend
    """

    description: str  # not sent to backend, fyi only
    upstream_url: str  # full url/route to the backend
    authorized_api_key: str  # not sent to the backend, used to check if client is allowed to contact this proxy
    query_params: dict[str, str | bool] = {}  # processing param sent to backend
    custom_headers: dict[str, str] = {}  # secrets sent to backend


settings = Settings()

VERSION_CONFIGS: dict[str, ProcessingConfig] = {
    "dev": ProcessingConfig(
        description="Dummy backend for development",
        upstream_url="http://localhost:9999/process",
        authorized_api_key="client-dev-api-key",
        query_params={"param_1": "value_1", "param_2": "value_2"},
        custom_headers={"Authorization": "Bearer backend-dev-api-key"},
    ),
    "v1": ProcessingConfig(
        description="Marker without llm",
        upstream_url=settings.UPSTREAM_V1_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V1,
        query_params={
            "use_llm": False,
            "force_ocr": True,
            "strip_existing_ocr": True,
            "output_format": "markdown",
        },
        custom_headers={"Authorization": f"Bearer {settings.UPSTREAM_V1_API_KEY}"},
    ),
    "v2": ProcessingConfig(
        description="Marker with qwen3-vl and image description",
        upstream_url=settings.UPSTREAM_V2_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V2,
        query_params={
            "use_llm": "OpenAI",
            "llm_service": "marker.services.openai.OpenAIService",
            "openai_base_url": settings.UPSTREAM_V2_VLLM_URL,
            "openai_model": "Qwen3-VL-30B-A3B-Instruct",
            "disable_image_extraction": True,
            "force_ocr": True,
            "strip_existing_ocr": True,
            "output_format": "markdown",
        },
        custom_headers={
            "Authorization": f"Bearer {settings.UPSTREAM_V2_API_KEY}",
            "openai_api_key": settings.UPSTREAM_V2_VLLM_API_KEY,
        },
    ),
    "v3": ProcessingConfig(
        description="Chandra",
        upstream_url=settings.UPSTREAM_V3_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V3,
        custom_headers={"Authorization": f"Bearer {settings.UPSTREAM_V3_API_KEY}"},
    ),
}


SUPPORTED_VERSIONS = tuple(VERSION_CONFIGS.keys())
Version = Literal[tuple(SUPPORTED_VERSIONS)]  # type: ignore[misc]
