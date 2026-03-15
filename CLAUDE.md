# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What MarkGate Is

MarkGate is a FastAPI proxy gateway between open-webui (ExternalDocumentLoader) and multiple document-to-Markdown backends (Paddle OCR, Marker, Chandra, Docling). It caches results persistently in S3 using content-based SHA256 hashing and prevents duplicate processing with Redis distributed locks.

Single public endpoint: `PUT /md/{version}/process` — accepts raw file bytes, returns Markdown + metadata.

## Commands

### Development Setup
```bash
uv venv && uv sync   # Install all dependencies including dev
```

### Linting and Type Checking
```bash
uv run ruff check src/          # Lint
uv run ruff format src/         # Format
uv run mypy src/                # Type check (strict mode)
```

### Running Locally
```bash
# Requires .env or .env_secret with all required vars (see docker/.env.example)
uv run uvicorn markgate.main:app --host 0.0.0.0 --port 8080 --reload
```

### Docker
```bash
docker build -f Dockerfile -t markgate:<tag> .
docker compose -f docker/compose.yaml up          # Production stack
docker compose -f docker/compose.yaml --profile dev-tools-docling up  # With Docling backend
```

## Architecture

### Request Flow
1. Client sends `PUT /md/{version}/process` with file bytes + `Content-Type` + `X-Filename` headers
2. `verify_api_key()` validates Bearer token against the version's expected key
3. SHA256 hash computed from file bytes
4. Redis lock acquired on the hash (prevents concurrent duplicate processing)
5. S3 checked for cached result at `documents/{hash}/{version}/content.md`
   - **Cache hit**: return cached Markdown + metadata immediately
   - **Cache miss**: call upstream backend → store in S3 → return response
6. Background task uploads source file, filename aliases, and per-version cache metadata to S3

### Module Responsibilities
- **`main.py`**: FastAPI app, single route handler, lifespan wiring
- **`config.py`**: `Settings` (pydantic-settings, all env vars), `Version` enum (v1.0.0–v4.1.0), `ProcessingConfig` (per-version backend URL + auth + query params)
- **`schemas.py`**: Pydantic v2 models — `ExternalDocumentRequestHeaders`, `ProcessedDocument` (backend response with images), `ResponseDocument` (client response, no images), `Metadata`
- **`services.py`**: `compute_hash()`, `call_upstream_backend()` (version-based routing), `update_s3_processed()`, `background_update_s3()`
- **`utils.py`**: `S3Manager` + `RedisManager` (async client lifecycle), all S3 I/O helpers, `verify_api_key()`, `lifespan` context manager

### S3 Layout
```
documents/{sha256_hash}/
  source.{ext}           # Original file
  _aliases.json          # Known filenames for this content
  {version}/
    content.md           # Converted Markdown
    metadata.json        # Backend-provided metadata
    _metadata.json       # Cache hit count, timestamps
    images/              # Extracted images (jpg/png)
```

### Key Design Constraints (from AGENTS.md)
- **Redis is used exclusively for distributed locking** — not for caching or persistence
- **S3 is the single source of truth** for all cached results
- The proxy must be stateless except for the S3Manager/RedisManager singletons initialized at lifespan
- Fail fast: propagate upstream errors without swallowing them

## Configuration

All config is environment-driven via `.env` and `.env_secret` (both loaded by pydantic-settings). See `docker/.env.example` for the full variable list. Key groups:
- `CLIENT_API_KEY_V{version}` — per-version API keys for clients
- `S3_*` — S3-compatible storage (tested with Garage)
- `REDIS_*` — Redis/Valkey connection and lock timeouts
- `UPSTREAM_V{version}_URL` / `UPSTREAM_V{version}_API_KEY` — backend routing

## Python Version

Requires exactly Python 3.14 (pinned in `.python-version`). Uses `uv` for dependency management.
