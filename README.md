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

### Endpoints

`{backend}` is the backend name as declared in `backend_config.toml` (e.g. `foil`, `docling`).

| Route | Images | Response |
|---|---|---|
| `PUT /md/{backend}/process` | no | `{ "page_content": "...", "metadata": { ... } }` |
| `PUT /{backend}/process` | yes (base64) | `{ "page_content": "...", "images": { "name": "b64..." }, "metadata": { ... } }` |
| `PUT /{backend}/process/download` | yes (files) | tar.zst archive (content.md + images + metadata.json) |

**Optional** — only present when auto backend selection is enabled (see below): `PUT
/md/auto/process`, `PUT /auto/process`, `PUT /auto/process/download`. Same request/response shapes
as the three routes above, but `{backend}` is picked automatically instead of named in the URL.

- **Body**: raw file bytes (`application/octet-stream`)
- **Headers**:
  - `Authorization: Bearer <authorized_api_key>` — key specific to the backend (see `backend_config.toml`)
  - `Content-Type` — declared MIME type (the app always re-detects from bytes; this is informational only)
  - `X-Filename` — URL-encoded original filename (e.g. `my%20report.pdf`)

Force re-processing (bypass cache), on any of the three routes:

```
PUT /md/{backend}/process?force_reprocess=true
```

#### Per-request overrides (`foil` backends only)

Foil-backed Versions also accept two optional query params, forwarded to Foil-Serve, on all three
routes above:

| Param | Values | Effect |
|---|---|---|
| `spreadsheet_mode` | `auto` (default) / `pandas` / `ocr` / `both` | Spreadsheet conversion strategy. No-op for non-spreadsheet files. |
| `excel_min_output_ratio` | float ≥ 0 | Overrides the sparse-output threshold that triggers PDF+OCR fallback in `auto` mode. No-op outside `auto` mode. |

```
PUT /md/foil/process?spreadsheet_mode=ocr
```

#### Static route aliases

Some clients can't set query params at all. Any `foil`-backed entry in `backend_config.toml` can
declare static route aliases — each one pins a preset of the overrides above and exposes it under
`/alias/...`, with the alias name inserted between the backend name and `process` so every route
still ends in `/process` (or `/process/download`):

```toml
[backends.foil]
...

[[backends.foil.aliases]]
name = "spreadsheet_ocr"
query_params = { spreadsheet_mode = "ocr" }
```

exposes, in addition to the three routes above:

| Route |
|---|
| `PUT /alias/md/foil/spreadsheet_ocr/process` |
| `PUT /alias/foil/spreadsheet_ocr/process` |
| `PUT /alias/foil/spreadsheet_ocr/process/download` |

each identical to calling `foil` with `?spreadsheet_mode=ocr` — same upstream call, same API key.
**An alias route and the equivalent query-param call share the same S3 cache entry and the same
processing lock** — there's no manual mapping to keep in sync: both resolve to the exact same
params sent to Foil-Serve, and the cache/lock key is derived from those resolved params rather
than from the Version name (see `ProcessingConfig.cache_key()` / `FoilConfig.cache_key()` in
`contracts.py` / `backends/foil.py`). Alias declarations are validated at startup — the app
refuses to start if an alias's `query_params` don't validate, or if two aliases on the same
backend share a `name`.

### Auto backend selection (optional)

Instead of naming a backend in the URL, `PUT /md/auto/process` (and its `/auto/process` /
`/auto/process/download` counterparts) let MarkGate pick one itself, based on properties of the
uploaded file (extension, size, …). **Off by default** — the routes don't exist at all unless
explicitly enabled — and the decision logic lives in a Python file you supply, outside MarkGate's
own code, so it can be changed without touching or redeploying the app.

Enable it with:

| Variable / key         | Default | Description                                                              |
|-------------------------|---------|---------------------------------------------------------------------------|
| `auto_route_enabled`    | `false` | Registers the 3 `/auto/*` routes when `true`.                            |
| `auto_selector_path`    | —       | Filesystem path to the `.py` file implementing `select()` (below).       |
| `client_api_key_auto`   | —       | Bearer token for `/auto/*`. Independent of any backend's own key, since the backend isn't known until after the file is inspected. Empty = always rejected. |
| `max_upload_size_bytes` | none    | Reject uploads over this size with `413` *before* running any selection logic. |

The selector file must define a module-level function:

```python
async def select(ctx: BackendSelectionContext) -> BackendSelection: ...
```

`BackendSelectionContext` (input) carries `file_content`, `filename`, `extension`,
`declared_content_type`, `sniffed_mime`, `size_bytes`, and `available_versions` (the live
`VERSION_CONFIGS`, so the file can reference real backend names and introspect what's actually
declared). `BackendSelection` (output) carries a `version` and optional `overrides` — the same
`overrides` mechanism the query-param overrides and static aliases above already use — so a
selector can pick a different backend outright, or tweak an existing one's params, but never
fabricate an ad-hoc config. Raise `NoSuitableBackendError` for a file that legitimately can't be
routed (→ `422`); any other exception propagates as a `500` (fail fast, not swallowed).

A working example ships at `src/markgate/config/auto_selector_demo.py`:

```python
async def select(ctx: BackendSelectionContext) -> BackendSelection:
    if ctx.size_bytes < 10 * 1024 * 1024:
        return BackendSelection(version=Version("foil-ministral-3-3b"))
    return BackendSelection(
        version=Version("foil"),
        overrides=SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.AUTO, excel_min_output_ratio=0.99),
    )
```

If a custom selector needs its own dependencies (e.g. a PDF-parsing library for a page-count-based
policy), add them to the `auto-routing` dependency group in `pyproject.toml` rather than MarkGate's
core dependencies — that group is empty by default.

**Cache/lock coherence**: selection is a pure, local, in-memory decision that runs before any
Redis/S3/upstream-backend activity — resolving a `Version` (+ optional overrides) hands off to the
exact same cache/lock pipeline the explicit routes use. Two requests that resolve to the same
`(Version, overrides)` pair for the same file content always share the same S3 cache entry and
Redis lock, whether they came in via `/auto/*`, an explicit route, a query-param override, or a
static alias — auto-selection never causes duplicate upstream processing.

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
        │  PUT /md/{backend}/process  (or /{backend}/process[/download])
        ▼
   [ MarkGate ]
        │
        ├── verify_api_key()                    — check client Bearer token for this backend
        ├── config.with_overrides(overrides)    — apply per-request query-param overrides, if any
        ├── compute_hash() + get_mime_type()    — parallel, from raw bytes
        ├── config.cache_key(version, mime)     — resolved-params signature (see below)
        ├── Redis lock (hash + signature)       — prevent concurrent duplicate processing
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

`config.cache_key(version, mime)` — default: the Version's `cache_id` (or its name), same as today.
`FoilConfig` overrides it to key on the *resolved* query params instead, so a preset Version, a
route alias, and an equivalent per-request override (see "Per-request overrides" above) all
converge on one cache entry.

### Module responsibilities

| Module                   | Role                                                                                                  |
|--------------------------|-------------------------------------------------------------------------------------------------------|
| `main.py`                | FastAPI app, route handlers, lifespan wiring                                                          |
| `config/settings.py`     | `Settings` (pydantic-settings) — infrastructure env vars (S3, Redis, logging, paths)                 |
| `config/loader.py`       | Builds the dynamic `Version` enum, `VERSION_CONFIGS`, and resolves/validates route aliases (`ALIAS_ROUTES`) from `backend_config.toml` at startup |
| `contracts.py`           | `ProcessingConfig` base class (`extra="forbid"`, `with_overrides()`, `cache_key()` extension points), `resolve_env_placeholders()` — shared between `config/` and `backends/` |
| `backends/__init__.py`   | `BACKEND_HANDLERS` registry, `BackendConfig` root TOML schema, `AnyProcessingConfig` union           |
| `backends/foil.py`       | Foil-serve handler + `FoilConfig` + `FoilRouteAlias` (static route aliases)                          |
| `backends/docling.py`    | Docling-serve handler + `DoclingConfig`                                                               |
| `backends/marker.py`     | Marker handler stub                                                                                   |
| `backends/chandra.py`    | Chandra handler stub                                                                                  |
| `schemas.py`             | Pydantic v2 models: request headers, `ProcessedDocument` (internal, PIL images), `ProcessedDocumentOut` (public, base64 images), `ResponseDocument`, `Metadata` |
| `services.py`            | Core logic: hash + MIME detection, cache resolution, upstream call, S3 writes, header merging        |
| `storage.py`             | `S3Manager` + `RedisManager` lifecycle, all S3 I/O helpers, `lifespan` context manager               |
| `security.py`            | `verify_api_key()` FastAPI dependency, `make_alias_api_key_verifier()` for route alias routes, `verify_api_key_auto()` for `/auto/*` |
| `media.py`               | PIL serialization, base64 helpers, libmagic MIME detection, `mime_to_ext()`, tar.zst builder         |
| `routing.py`             | Optional auto backend selection: `BackendSelectionContext`/`BackendSelection` contract, `load_selector()` |
| `auto_routes.py`         | Optional `/auto/*` routes — built only when `auto_route_enabled` is true (see `routing.py`)          |

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