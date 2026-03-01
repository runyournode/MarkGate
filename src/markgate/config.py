from typing import Optional, List
from enum import Enum
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import json

# used to call marker processing backends (not oin prod yet)
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
    S3_ACCESS_KEY: str = "GK4bfd698ae1e5d64fb82cda99"
    S3_SECRET_KEY: str = "9da62e93199a79737603cbc46b05fd03412e582386f0cb19795aec7386c32f9e"
    S3_BUCKET: str = "markgate-cache"
    S3_REGION: str = "garage"

    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # --- INCOMING AUTHENTICATION (Client -> Proxy) ---
    # Keys that clients (e.g. Open WebUI) must provide to use this proxy
    CLIENT_API_KEY_V100: str = "changeme"
    CLIENT_API_KEY_V110: str = "changeme"
    CLIENT_API_KEY_V120: str = "changeme"

    CLIENT_API_KEY_V200: str = "changeme"
    CLIENT_API_KEY_V300: str = "changeme"
    CLIENT_API_KEY_V400: str = "changeme"


    # --- UPSTREAM CONFIGURATION (Proxy -> Backend) ---

    # V1: paddleocrvl_server
    UPSTREAM_V100_URL: str = "http://localhost:8081/v1/process"
    UPSTREAM_V100_API_KEY: str = "changeme"  # Key for the V1 backend

    # V1: paddleocrvl_server + ministral-3-3b
    UPSTREAM_V110_URL: str = "http://localhost:8081/v1/process"
    UPSTREAM_V110_API_KEY: str = "changeme"  # Key for the V1 backend

    # V1: paddleocrvl_server + ministral-3-14b
    UPSTREAM_V120_URL: str = "http://localhost:8081/v1/process"
    UPSTREAM_V120_API_KEY: str = "changeme"  # Key for the V1 backend

    # V2: Marker with qwen3-vl and image description
    UPSTREAM_V2_URL: str = "http://localhost:9001/process"
    UPSTREAM_V2_API_KEY: str = ""  # Key for the V2 backend
    UPSTREAM_V2_VLLM_URL: str = ""
    UPSTREAM_V2_VLLM_API_KEY: str = ""  # Key for the LLM service used by V2

    # V3: Chandra
    UPSTREAM_V3_URL: str = "http://localhost:9002/process"
    UPSTREAM_V3_API_KEY: str = ""  # Key for the LLM service used by V3

    # V4: Docling (test!)
    UPSTREAM_V4_URL: str = "http://localhost:5001/v1/convert/file"
    UPSTREAM_V4_API_KEY: str = "toto"  # Key for the V4 backend
    UPSTREAM_V4_VLLM_URL: str = "http://vllm_dumu_url:999"
    UPSTREAM_V4_VLLM_API_KEY: str = "toto"  # Key for the LLM service used by V4

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
        str, str | int | float | bool | dict | List[str]
    ] = {}  # processing param sent to backend
    custom_headers: dict[str, str] = {}  # secrets sent to backend


# Supported versions
class Version(str, Enum):
    """
    Nomenclature des versions : v_X_Y_Z
    X : Moteur principal du backend de Layout + OCR (ex: paddle, marker, docling, ...)
    Y : Changement Radical du backend (ex: VLM utilisé en plus pour la description d'image)
    Z : Incréments éventuels (correction bug majeur, ...)

    Pour le dev utiliser V
    """

    # Padlle

    v_1_0_0 = "v1.0.0"
    v_1_1_0 = "v1.1.0" # + ministral-3-3b
    v_1_2_0 = "v1.2.0"  # + ministral-3-14b

    # Marker
    v_2_0_0 = "v2.0.0"
    v_2_1_0 = "v2.1.0"
    v_2_2_0 = "v2.2.0"  # document extractor (dedicated video processing)

    # Chandra
    v_3_0_0 = "v3.0.0"

    # Docling
    v_4_0_0 = "v4.0.0"
    v_4_1_0 = "v4.1.0"

    # For dev (upstream configuration can change at any time)
    v_1_dev = "v1-dev"


# -------------------------------------------------
# CONFIGURATION FOR THE PROCESSING BACKENDS       -
# When adding new conf, use json.dumps() for      -
# nested dict in query_params  (e.g V4)           -
# -------------------------------------------------
settings = Settings()
VERSION_CONFIGS: dict[Version, ProcessingConfig] = {

    Version.v_1_0_0: ProcessingConfig(
        description="paddleocrvl_server (sans image description)",
        upstream_url=settings.UPSTREAM_V100_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V100,
        query_params={},
        custom_headers={
            "Content-Type": "application/octet-stream",
            "Authorization": f"Bearer {settings.UPSTREAM_V100_API_KEY}",
        },
    ),

    Version.v_1_1_0: ProcessingConfig(
            description="paddleocrvl_server avec description image par ministral-3-3b",
            upstream_url=settings.UPSTREAM_V110_URL,
            authorized_api_key=settings.CLIENT_API_KEY_V110,
            query_params={
                "image_description_model_name": "ministral-3-3b",
            },
            custom_headers={
                "Content-Type": "application/octet-stream",
                "Authorization": f"Bearer {settings.UPSTREAM_V110_API_KEY}",
            },
        ),

    Version.v_1_2_0: ProcessingConfig(
                description="paddleocrvl_server avec description image par ministral-3-3b",
                upstream_url=settings.UPSTREAM_V120_URL,
                authorized_api_key=settings.CLIENT_API_KEY_V120,
                query_params={
                    "image_description_model_name": "ministral-3-14b",
                },
                custom_headers={
                    "Content-Type": "application/octet-stream",
                    "Authorization": f"Bearer {settings.UPSTREAM_V110_API_KEY}",
                },
            ),



    # ALL VERSION ARE SUBJECT TO CHANGE - NOT FOR PRODUCTION USE


    Version.v_1_dev: ProcessingConfig(
        description="paddleocrvl_server (pour le dev)",
        upstream_url="http://localhost:8081/v1/process",
        authorized_api_key="changeme",
        query_params={
            "image_description_model_name": "dev_vlm"
        },
        custom_headers={
            "Content-Type": "application/octet-stream",
            "Authorization": f"Bearer {settings.UPSTREAM_V100_API_KEY}",
        },
    ),


    Version.v_2_0_0: ProcessingConfig(
        description="Marker native with qwen3-vl and image description",
        upstream_url=settings.UPSTREAM_V2_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V200,
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
    Version.v_3_0_0: ProcessingConfig(
        description="Chandra",
        upstream_url=settings.UPSTREAM_V3_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V300,
        custom_headers={"Authorization": f"Bearer {settings.UPSTREAM_V3_API_KEY}"},
    ),

    Version.v_4_0_0: ProcessingConfig(
        description="Docling test",
        upstream_url=settings.UPSTREAM_V4_URL,
        authorized_api_key=settings.CLIENT_API_KEY_V400,
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
            # "ocr_engine": "easyocr",
            "ocr_engine": "tesseract",
            "table_mode": "accurate",
            "abort_on_error": True,  # effet ?
            "do_formula_enrichment": True,
            "do_picture_description": True,
            "picture_description_area_threshold": 0.01,
            "picture_description_api": json.dumps(
                {
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
                }
            ),
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
        },
        custom_headers={"X-Api-Key": settings.UPSTREAM_V4_API_KEY},
    ),

}

