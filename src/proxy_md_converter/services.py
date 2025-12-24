import hashlib
import json
import boto3
import redis
import httpx
from typing import TYPE_CHECKING, Any
from datetime import datetime
from pathlib import Path as FilePath
from botocore.exceptions import ClientError
from redis import Redis

from .config import (
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, 
    REDIS_HOST, REDIS_PORT
)
from .models import VersionMetadata, GlobalFileAliases

# --- IMPORT CONDITIONNEL ---
if TYPE_CHECKING:
    # Cet import ne se fait QUE si on lance mypy.
    # Au runtime (prod), cette ligne est ignorée, donc pas de crash.
    from mypy_boto3_s3 import S3Client
else:
    # Au runtime, on peut utiliser Any ou object pour tromper l'interpréteur si besoin
    # Mais ici on n'en a même pas besoin car boto3.client renvoie un proxy dynamique.
    S3Client = Any

# --- Clients ---

# On annote avec 'S3Client' qui est défini soit via mypy_boto3 (dev) soit comme Any (prod)
s3_client: S3Client = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY
)

redis_client: Redis = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True
)

def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def get_extension(filename: str) -> str:
    return FilePath(filename).suffix.lower()

def s3_key_exists(key: str) -> bool:
    try:
        s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError:
        return False

def background_update_task(file_hash: str, version: str, filename: str, content: bytes, content_type: str) -> None:
    try:
        ext: str = get_extension(filename) or ".bin"
        source_key: str = f"documents/{file_hash}/source{ext}"
        
        if not s3_key_exists(source_key):
            s3_client.put_object(
                Bucket=S3_BUCKET, Key=source_key, Body=content,
                ContentType=content_type, Metadata={"original_name": filename}
            )

        # Alias logic...
        alias_key: str = f"documents/{file_hash}/_aliases.json"
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=alias_key)
            raw_data: str = obj["Body"].read().decode("utf-8")
            aliases = GlobalFileAliases.model_validate_json(raw_data)
            if filename not in aliases.filenames:
                aliases.filenames.append(filename)
                s3_client.put_object(Bucket=S3_BUCKET, Key=alias_key, Body=aliases.model_dump_json())
        except ClientError:
            new_aliases = GlobalFileAliases(file_hash=file_hash, filenames=[filename])
            s3_client.put_object(Bucket=S3_BUCKET, Key=alias_key, Body=new_aliases.model_dump_json())

        # Metadata logic...
        meta_key: str = f"documents/{file_hash}/{version}/metadata.json"
        now: datetime = datetime.utcnow()
        try:
            obj_meta = s3_client.get_object(Bucket=S3_BUCKET, Key=meta_key)
            raw_meta: str = obj_meta["Body"].read().decode("utf-8")
            meta = VersionMetadata.model_validate_json(raw_meta)
            meta.last_hit_at = now
            meta.hit_count += 1
            meta.last_filename_used = filename
        except ClientError:
            meta = VersionMetadata(
                version=version, created_at=now, last_hit_at=now, 
                hit_count=1, last_filename_used=filename
            )
        s3_client.put_object(Bucket=S3_BUCKET, Key=meta_key, Body=meta.model_dump_json())

    except Exception as e:
        print(f"Error in background task: {e}")

async def call_upstream_backend(url: str, content: bytes, headers: dict[str, str], params: dict[str, str]) -> dict:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.put(url, content=content, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()
