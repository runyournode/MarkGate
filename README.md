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
- Exposes versioned endpoints so you can run multiple backends or configurations in parallel

### Supported backends

| Version group       | Backend                                                 | Status                     |
|---------------------|---------------------------------------------------------|----------------------------|
| `v1.0.0` – `v1.3.0` | [Foil Serve](https://github.com/RunYourNode/foil-serve) | **Production-ready** (ish) |
| `v4.0.0`            | Docling (docling-serve)                                 | Tested in early stages     |
| `v2.x.x`            | Marker                                                  | Planned (or maybe not)     |
| `v3.x.x`            | Chandra                                                 | Planned (or maybe not)     |

### Endpoint

```
PUT /md/{version}/process
```

- **Body**: raw file bytes (`application/octet-stream`)
- **Headers**:
  - `Authorization: Bearer <CLIENT_API_KEY>` — key specific to the version (see config)
  - `Content-Type` — declared MIME type (the app always re-detects from bytes; this is informational only)
  - `X-Filename` — URL-encoded original filename (e.g. `my%20report.pdf`)
- **Response**: `{ "page_content": "...", "metadata": { ... } }`

A second endpoint returns a downloadable archive (content.md + images + metadata):

```
PUT /md/{version}/process/download   →   tar.zst archive
```

Force re-processing (bypass cache):

```
PUT /md/{version}/process?force_reprocess=true
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

# With Docling backend (untested in latest developpement stages)
docker compose -f docker/compose.yaml --profile dev-tools-docling up
```

Configuration lives in `docker/.env.d/markgate.env` (copy from `docker/.env.example`).

### Configuration reference

All variables are loaded from `.env` and `.env_secret` (both optional, merged).

**Client authentication** (Open WebUI → MarkGate):

| Variable                                      | Description                                       |
|-----------------------------------------------|---------------------------------------------------|
| `CLIENT_API_KEY_V100` … `CLIENT_API_KEY_V400` | Bearer token expected from the client per version |

**S3 cache** (any S3-compatible storage, tested with [Garage](https://garagehq.deuxfleurs.fr/)):


| Variable                          | Default                 | Description                             |
|-----------------------------------|-------------------------|-----------------------------------------|
| `S3_ENDPOINT`                     | `http://localhost:3900` | S3 endpoint URL                         |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | —                       | Credentials                             |
| `S3_BUCKET`                       | `markgate-cache`        | Bucket name                             |
| `S3_CACHE_ENABLED`                | `true`                  | Set `false` to disable caching entirely |

**Redis / Valkey**:

| Variable                    | Default              | Description                                           |
|-----------------------------|----------------------|-------------------------------------------------------|
| `REDIS_HOST` / `REDIS_PORT` | `localhost` / `6379` | Connection                                            |
| `REDIS_LOCK_TIMEOUT`        | `300`                | Lock TTL in seconds (auto-extended during processing) |
| `REDIS_BLOCKING_TIMEOUT`    | `9999999`            | How long to wait for a lock before returning 504      |

**Upstream backends**:

| Variable                                          | Description                                            |
|---------------------------------------------------|--------------------------------------------------------|
| `UPSTREAM_V100_URL`                               | Full URL to the foil-serve endpoint                    |
| `UPSTREAM_V100_API_KEY`                           | API key sent to the backend (never exposed to clients) |
| *(same pattern for V110, V120, V130, V2, V3, V4)* |                                                        |

**Failed requests archiving** (for debugging):

| Variable                    | Default                | Description                            |
|-----------------------------|------------------------|----------------------------------------|
| `FAILED_REQUESTS_S3_PREFIX` | `failed_requests`      | S3 prefix for failed request artifacts |
| `FAILED_REQUESTS_LOCAL_DIR` | `/tmp/markgate_failed` | Local fallback when S3 is unavailable  |

### S3 bucket layout

```
📂 S3 Bucket
├── 📂 documents/
│   └── 📂 {sha256}/
│       ├── 📄 source.{ext}          # Original file (extension from detected MIME type)
│       ├── 📄 _aliases.json         # All filenames seen for this content
│       └── 📂 {version}/
│           ├── 📄 content.md        # Converted Markdown
│           ├── 📄 metadata.json     # Backend-provided metadata
│           ├── 📄 _metadata.json    # Cache hit count, timestamps, last filename
│           └── 📂 images/           # Extracted images (jpg/png/…)
└── 📂 failed_requests/
    └── 📂 {timestamp}_{hash}_{version}/
        ├── 📄 source.{ext}          # File that failed
        └── 📄 error.json            # Error message, upstream duration, context
```

---

## For Developers

### Architecture

```
Client (e.g., Open WebUI)
        │  PUT /md/{version}/process
        ▼
   [ MarkGate ]
        │
        ├── verify_api_key()                    — check client Bearer token for this version
        ├── compute_hash() + get_mime_type()    — parallel, from raw bytes
        ├── Redis lock (hash + version)         — prevent concurrent duplicate processing
        │
        ├── S3 cache hit?  ──yes──►  return cached content.md
        │
        └── no ──► call_upstream_backend()
                        │
                        ├── _merge_headers()   — strip client auth, merge with config.custom_headers
                        └── POST to backend    — foil-serve / docling / …
                                │
                                ▼
                        update_s3_processed()  — write content.md, metadata, images
                        background_update_s3() — write source file, _aliases, _metadata
```

### Module responsibilities

| Module        | Role                                                                                                      |
|---------------|-----------------------------------------------------------------------------------------------------------|
| `main.py`     | FastAPI app, route handlers, lifespan wiring                                                              |
| `config.py`   | `Settings` (env vars), `Version` enum, `ProcessingConfig` (per-version backend URL + auth + query params) |
| `schemas.py`  | Pydantic v2 models: request headers, response, internal document, S3 metadata                             |
| `services.py` | Core logic: hash + MIME detection, cache resolution, upstream call, S3 writes, header merging             |
| `storage.py`  | `S3Manager` + `RedisManager` lifecycle, all S3 I/O helpers, `lifespan` context manager                    |
| `security.py` | `verify_api_key()` FastAPI dependency                                                                     |
| `media.py`    | PIL serialization, base64 helpers, libmagic MIME detection, `mime_to_ext()`, tar.zst builder              |

### Key design decisions

- **MIME type is always detected from bytes** via libmagic — the client-declared `Content-Type` is never trusted. The detected MIME is used for the S3 `ContentType`, the S3 key extension, and the upstream `Content-Type` header.
- **Redis is used exclusively for distributed locking** — not for caching or persistence. S3 is the single source of truth.
- **Client auth headers are never forwarded** to upstream backends (`Authorization` is stripped). Each backend version has its own credentials defined in `ProcessingConfig.custom_headers`.
- **Header consolidation**: upstream headers (with detected MIME overriding Content-Type) are merged with `config.custom_headers`; the config always wins on conflicts.
- **The proxy is stateless** except for the `S3Manager`/`RedisManager` singletons initialized at lifespan.
- **Fail fast**: upstream errors are propagated to the client (502), artifacts saved to `failed_requests/` for debugging.

### Adding a new backend

1. Add a new `Version` enum value in `config.py`
2. Add a `ProcessingConfig` entry in `VERSION_CONFIGS` with the backend URL, client API key, and backend credentials in `custom_headers`
3. Add a `case Version.vX_Y_Z:` branch in `call_upstream_backend()` in `services.py` that calls the backend and returns a `ProcessedDocument`

### Development setup

Requires Python 3.14 and `uv`.

```bash
uv venv && uv sync          # install all dependencies including dev

# Run locally (requires .env or .env_secret)
cd src/margate 
uv run uvicorn markgate.main:app --host 0.0.0.0 --port 8080 --reload


# Lint / format / type check
uv run ruff check src/
uv run ruff format src/
uv run ty check src/
```