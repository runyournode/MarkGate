import os
from typing import TypedDict

class ProcessingConfig(TypedDict):
    description: str
    query_params: dict[str, str]
    custom_headers: dict[str, str]

S3_ENDPOINT: str = os.getenv("S3_ENDPOINT", "http://localhost:3900")
S3_ACCESS_KEY: str = os.getenv("S3_ACCESS_KEY", "admin")
S3_SECRET_KEY: str = os.getenv("S3_SECRET_KEY", "password")
S3_BUCKET: str = os.getenv("S3_BUCKET", "doc-cache")

REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
UPSTREAM_URL: str = os.getenv("UPSTREAM_URL", "http://localhost:8000")

VERSION_CONFIGS: dict[str, ProcessingConfig] = {
    "v1": {
        "description": "Standard",
        "query_params": {"mode": "fast", "ocr": "false"},
        "custom_headers": {"X-Processing-Model": "basic-v1"}
    },
    "v2": {
        "description": "OCR",
        "query_params": {"mode": "high_res", "ocr": "true", "lang": "fr"},
        "custom_headers": {"X-Processing-Model": "gpt-ocr-v2"}
    }
}
