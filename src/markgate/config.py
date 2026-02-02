from typing import Optional, List
from enum import Enum
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


image_description_prompt = """You are a document analysis expert who specializes in creating text descriptions for images.
You will receive an image of a picture or figure.  Your job will be to create a short description of the image.
**Instructions:**
1. Carefully examine the provided image.
2. Output a faithful description of the image.  Make sure there is enough specific detail to accurately reconstruct the image.
If the image is a figure or contains alpha-numeric data, include the alpha-numeric data in the output.
**Example Output:
In this figure, a bar chart titled "Fruit Preference Survey" is showing the number of people who prefer different types of fruits.  The x-axis shows the types of fruits, and the y-axis shows the number of people.  The bar chart shows that most people prefer apples, followed by bananas and oranges.  20 people prefer apples, 15 people prefer bananas, and 10 people prefer oranges.
"""


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
    CLIENT_API_KEY_V4: str = "client-secret-v4"

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

    # V4: Docling (test!)
    UPSTREAM_V4_URL: str = "http://dockling-serve:5001/v1/convert/file"
    UPSTREAM_V4_API_KEY: str = ""  # Key for the V4 backend
    UPSTREAM_V4_VLLM_URL: str = ""
    UPSTREAM_V4_VLLM_API_KEY: str = ""  # Key for the LLM service used by V4

    # todo: a tester que les var env (e.g. export ou celles passées par docker compose)
    #  prennent le dessus sur celles definies dans ce .py ou dans le .env)
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
    query_params: dict[
        str, str | bool | dict | List | float
    ] = {}  # processing param sent to backend
    custom_headers: dict[str, str] = {}  # secrets sent to backend


settings = Settings()


# Supported versions
class Version(str, Enum):
    DEV = "dev"
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"
    V4 = "v4"


VERSION_CONFIGS: dict[Version, ProcessingConfig] = {
    Version.DEV: ProcessingConfig(
        description="Dummy backend for development",
        upstream_url="http://localhost:9999/process",
        authorized_api_key="client-dev-api-key",
        query_params={"param_1": "value_1", "param_2": "value_2"},
        custom_headers={"Authorization": "Bearer backend-dev-api-key"},
    ),
    Version.V1: ProcessingConfig(
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
    Version.V2: ProcessingConfig(
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
    Version.V3: ProcessingConfig(
        description="Chandra",
        upstream_url=settings.UPSTREAM_V3_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V3,
        custom_headers={"Authorization": f"Bearer {settings.UPSTREAM_V3_API_KEY}"},
    ),
    Version.V4: ProcessingConfig(
        description="Docling test",
        upstream_url=settings.UPSTREAM_V4_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V4,
        query_params={
            # "from_formats": [...] default to all format
            "to_formats": ["md"],
            "image_export_mode": "placeholder",
            "include_images": False,
            "do_ocr": True,
            "force_ocr": True,
            "ocr_lang": ["en"],
            "pdf_backend": "dlparse_v4",
            "table_cell_matching": False,
            "do_formula_enrichment": True,
            "do_picture_description": True,
            "picture_description_area_threshold": 0.01,
            "picture_description_api": {
                "url": f"{settings.UPSTREAM_V4_VLLM_URL}",
                "headers": {
                    "Authorization": f"Bearer {settings.UPSTREAM_V4_VLLM_API_KEY}",
                },
                "params": {
                    "model": "stepfun-ai/Step3-VL-10B",
                    "max_completion_tokens": 500,
                },
                "timeout": 60,
                "concurrency": 10,
                "prompt": image_description_prompt,
            },
            # "vlm_pipeline_model_api": {
            #     "url": f"{settings.UPSTREAM_V4_VLLM_URL}",
            #     "headers": {
            #         "Authorization": f"Bearer {settings.UPSTREAM_V4_VLLM_API_KEY}",
            #     },
            #     "params": {"model": "stepfun-ai/Step3-VL-10B"},
            #     "prompt": image_description_prompt,
            #     "temperature": 0.0,
            #     "response_format": "",
            #     "concurrency": 10,
            # },
            # default params
            "ocr_engine": "easyocr",
            "table_mode": "accurate",
            "abort_on_error": False,
        },
        custom_headers={"Authorization": f"Bearer {settings.UPSTREAM_V4_API_KEY}"},
    ),
}
