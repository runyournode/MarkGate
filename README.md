<div align="center">
<img height="200" src="src/markgate/statics/markgate_banner.jpg" title="MarkGate Banner"/>
</div>

# MarkGate

**MarkGate** is a proxy gateway between any HTTP client and document-to-Markdown conversion backends.

It provides persistent, content-addressed caching via S3 and prevents duplicate processing with Redis distributed locks.   
MarkGate is compatible with the [Open WebUI](https://github.com/open-webui/open-webui) `ExternalDocumentLoader` format.

---

## For Users & Operators

### What it does

- Accepts a raw file over HTTP and routes it to the appropriate backend converter
- Returns the converted Markdown (and optionally a tar.zst archive with images and metadata)
- Caches results in S3 by content hash — sending the same file twice never re-processes it
- Exposes named backend endpoints, so you can run multiple backends or configurations in parallel

### Supported backends

Backends are declared in `backend_config.toml` — the list below reflects the default config.

| Backend name          | Underlying service                                              | Notes                     |
|-----------------------|-----------------------------------------------------------------|---------------------------|
| `foil`                | [Foil Serve](https://github.com/RunYourNode/foil-serve)        | Production-ready          |
| `foil-ministral-3-3b` | Foil Serve + Ministral-3B VLM image description                | Production-ready     |
| `foil-qwen-3.5-27b`   | Foil Serve + Qwen-3.5-27B VLM image description                | Production-ready |
| `docling`             | Docling-serve (tesseract OCR)                                   | Tested in early stages    |
| `docling-vl`          | Docling-serve + VLM image description                           | Tested in early stages    |

Adding a new instance of an existing engine requires editing `backend_config.toml`. Adding a new engine type requires Python code (see developer section below).

### Endpoint

```
PUT /md/{backend}/process
```

`{backend}` is the backend name as declared in `backend_config.toml` (e.g. `foil`, `docling`).

- **Body**: raw file bytes (`application/octet-stream`)
- **Headers**:
  - `Authorization: Bearer <authorized_api_key>` — key specific to the backend (see `backend_config.toml`)
  - `Content-Type` — declared MIME type (the app always re-detects from bytes; this is informational only)
  - `X-Filename` — URL-encoded original filename (e.g. `my%20report.pdf`)
- **Response**: `{ "page_content": "...", "metadata": { ... } }`

A second endpoint returns a downloadable archive (content.md + images + metadata):

```
PUT /md/{backend}/process/download   →   tar.zst archive
```

Force re-processing (bypass cache):

```
PUT /md/{backend}/process?force_reprocess=true
```

### Health endpoints

| Route | Description |
|---|---|
| `GET /health` | Liveness — always 200 if the app is up |
| `GET /health/dependencies` | Redis, S3, and upstream backend status (200 / 207 / 503) |

### Running with Docker

```bash
# Production stack (MarkGate + Valkey/Redis)
docker compose -f docker/compose.yaml up

# With Docling backend
docker compose -f docker/compose.yaml --profile dev-tools-docling up
```

Configuration lives in `docker/mounts_config/markgate/config/`:

| File                | Purpose                                                   |
|---------------------|-----------------------------------------------------------|
| `backend_config.toml` | Backend declarations (URLs, API keys via `${VAR}`)      |
| `server_config.toml`  | Infrastructure settings (S3, Redis, logging, timeouts)  |
| `.env.secret`         | Secrets (S3 credentials, API keys) — never committed    |

### Configuration reference

**Infrastructure settings** (`server_config.toml` or environment variables):

All keys are case-insensitive. Environment variables take precedence over the TOML file.

**S3 cache** (any S3-compatible storage, tested with [Garage](https://garagehq.deuxfleurs.fr/)):

| Variable / key                    | Default                 | Description                             |
|-----------------------------------|-------------------------|-----------------------------------------|
| `s3_endpoint`                     | `http://localhost:3900` | S3 endpoint URL                         |
| `s3_access_key` / `s3_secret_key` | —                       | Credentials — set in `.env_secret`      |
| `s3_bucket`                       | `markgate-cache`        | Bucket name                             |
| `s3_cache_enabled`                | `true`                  | Set `false` to disable caching entirely |

**Redis / Valkey**:

| Variable / key              | Default              | Description                                           |
|-----------------------------|----------------------|-------------------------------------------------------|
| `redis_host` / `redis_port` | `localhost` / `6379` | Connection                                            |
| `redis_lock_timeout`        | `300`                | Lock TTL in seconds (auto-extended during processing) |
| `redis_blocking_timeout`    | `9999999`            | Max wait for a lock before returning 504              |

**Error reporting**:

| Variable / key   | Default | Description                                                                   |
|------------------|---------|-------------------------------------------------------------------------------|
| `verbose_errors` | `false` | Forward upstream error details to the client  |

**Config paths**:

| Variable / key        | Default                   | Description                              |
|-----------------------|---------------------------|------------------------------------------|
| `backend_config_path` | `backend_config.toml`     | Path to `backend_config.toml` (relative to CWD or absolute) |

**Backend configuration** (`backend_config.toml`):

Each `[backends.<name>]` section declares one endpoint. API keys are referenced as `${VAR_NAME}` and resolved from `.env_secret` or the environment. The application refuses to start if any referenced variable is missing.

```toml
[backends.foil]
backend_type = "foil"
description  = "Foil-serve — no image description"
upstream_url = "http://foil-serve:8081/v1/process"
authorized_api_key = "${CLIENT_API_KEY_FOIL}"   # client → MarkGate

[backends.foil.custom_headers]
Authorization = "Bearer ${UPSTREAM_FOIL_API_KEY}"  # MarkGate → backend
```

`cache_id` (optional): stable S3 path key — set to the old name when renaming a backend to preserve existing cache.

### S3 bucket layout

```
📂 S3 Bucket
├── 📂 documents/
│   └── 📂 {sha256}/
│       ├── 📄 source.{ext}          # Original file (extension from detected MIME type)
│       ├── 📄 _aliases.json         # All filenames seen for this content
│       └── 📂 {backend}/            # backend name (or cache_id if set)
│           ├── 📄 content.md        # Converted Markdown
│           ├── 📄 metadata.json     # Backend-provided metadata
│           ├── 📄 _metadata.json    # Cache hit count, timestamps, last filename
│           └── 📂 images/           # Extracted images (jpg/png/…)
└── 📂 failed_requests/
    └── 📂 {timestamp}_{hash}_{backend}/
        ├── 📄 source.{ext}          # File that failed
        └── 📄 error.json            # Error message, upstream duration, context
```

---

## For Developers

### Architecture

```
Client (e.g., Open WebUI)
        │  PUT /md/{backend}/process
        ▼
   [ MarkGate ]
        │
        ├── verify_api_key()                    — check client Bearer token for this backend
        ├── compute_hash() + get_mime_type()    — parallel, from raw bytes
        ├── Redis lock (hash + backend)         — prevent concurrent duplicate processing
        │
        ├── S3 cache hit?  ──yes──►  return cached content.md
        │
        └── no ──► call_upstream_backend()
                        │
                        ├── BACKEND_HANDLERS[config.backend_type]  — dispatch to backend module
                        ├── _merge_headers()   — strip client auth, merge with config.custom_headers
                        └── POST to backend    — foil / docling / …
                                │
                                ▼
                        update_s3_processed()  — write content.md, metadata, images
                        background_update_s3() — write source file, _aliases, _metadata
```

### Module responsibilities

| Module                   | Role                                                                                                  |
|--------------------------|-------------------------------------------------------------------------------------------------------|
| `main.py`                | FastAPI app, route handlers, lifespan wiring                                                          |
| `config/settings.py`     | `Settings` (pydantic-settings) — infrastructure env vars (S3, Redis, logging, paths)                 |
| `config/loader.py`       | Builds the dynamic `Version` enum and `VERSION_CONFIGS` from `backend_config.toml` at startup        |
| `contracts.py`           | `ProcessingConfig` base class, `resolve_env_placeholders()` — shared between `config/` and `backends/` |
| `backends/__init__.py`   | `BACKEND_HANDLERS` registry, `BackendConfig` root TOML schema, `AnyProcessingConfig` union           |
| `backends/foil.py`       | Foil-serve handler + `FoilConfig`                                                                     |
| `backends/docling.py`    | Docling-serve handler + `DoclingConfig`                                                               |
| `backends/marker.py`     | Marker handler stub                                                                                   |
| `backends/chandra.py`    | Chandra handler stub                                                                                  |
| `schemas.py`             | Pydantic v2 models: request headers, `ProcessedDocument`, `ResponseDocument`, `Metadata`             |
| `services.py`            | Core logic: hash + MIME detection, cache resolution, upstream call, S3 writes, header merging        |
| `storage.py`             | `S3Manager` + `RedisManager` lifecycle, all S3 I/O helpers, `lifespan` context manager               |
| `security.py`            | `verify_api_key()` FastAPI dependency                                                                 |
| `media.py`               | PIL serialization, base64 helpers, libmagic MIME detection, `mime_to_ext()`, tar.zst builder         |

### Key design decisions

- **MIME type is always detected from bytes** via libmagic — the client-declared `Content-Type` is never trusted. The detected MIME is used for the S3 `ContentType`, the S3 key extension, and the upstream `Content-Type` header.
- **Redis is used exclusively for distributed locking** — not for caching or persistence. S3 is the single source of truth.
- **Client auth headers are never forwarded** to upstream backends (`Authorization` is stripped). Each backend has its own credentials in `custom_headers`.
- **Header consolidation**: upstream headers (with detected MIME overriding Content-Type) are merged with `config.custom_headers`; the config always wins on conflicts.
- **The proxy is stateless** except for the `S3Manager`/`RedisManager` singletons initialized at lifespan.
- **Fail fast**: upstream errors are propagated to the client (502), artifacts saved to `failed_requests/` for debugging.
- **Backends are TOML-driven**: `Version` enum and `VERSION_CONFIGS` are built dynamically at startup from `backend_config.toml` — no code change needed to add or rename a backend.

### Adding a new backend

There are two distinct cases depending on whether the underlying **engine** already exists.

---

**Case 1 — new configuration for an existing engine** (TOML only, no code)

Use this when the engine is already supported (e.g. a new Foil instance with a different VLM model,
or a Docling variant with different OCR settings).

Add a `[backends.<name>]` section in `backend_config.toml`:

```toml
[backends.foil-my-model]
backend_type = "foil"                              # must match an existing BACKEND_HANDLERS key
description  = "Foil with my custom model"
upstream_url = "http://foil-serve:8081/v1/process"
authorized_api_key = "${CLIENT_API_KEY_MY_MODEL}"

[backends.foil-my-model.custom_headers]
Authorization = "Bearer ${UPSTREAM_FOIL_API_KEY}"

[backends.foil-my-model.query_params]
image_description_model_name = "my-model"
```

Add the referenced env vars to `.env_secret` and restart — the new endpoint
`PUT /md/foil-my-model/process` is live.

Currently supported engines (valid `backend_type` values): `foil`, `docling`, `marker`, `chandra`.

> **Production readiness**: `foil` is battle-tested in production. `docling` is functional but
> tested only in early stages. `marker` and `chandra` are stubs (not implemented).

---

**Case 2 — new backend engine** (Python code required)

Use this when you need to integrate a new HTTP service that has its own API contract.

1. Create `src/markgate/backends/myengine.py` with:
   - A `MyEngineConfig(ProcessingConfig)` subclass (declare typed `query_params` if needed)
   - An `async def call(config, file_content, headers, filename, client) -> ProcessedDocument` coroutine
2. Register it in `backends/__init__.py`:
   - Add `"myengine": myengine.call` to `BACKEND_HANDLERS`
   - Add `MyEngineConfig` to the `AnyProcessingConfig` union
3. Declare at least one instance in `backend_config.toml` with `backend_type = "myengine"` (Case 1 above).

### Development setup

Requires Python 3.14 and `uv`.

```bash
uv venv && uv sync          # install all dependencies including dev

# Run locally (requires config/backend_config.toml and config/server_config.toml or .env_secret)
uv run uvicorn markgate.main:app --host 0.0.0.0 --port 8080 --reload

# Lint / format / type check
uv run ruff check src/
uv run ruff format src/
uv run ty check src/
```