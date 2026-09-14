# MarkGate WebUI

Browser UI mounted by `main.py` on `/ui` when `enable_ui = true` (default).
Everything UI-related lives in this package; the API code knows nothing about it.

```
webui/
  __init__.py         GET /ui/backends.json (filtered view of backend_config.toml) + STATIC_DIR
  static/
    index.html        layout
    app.css           styles (light/dark)
    app.js            OpenAPI discovery, form, API keys, request, rendering
    param-rules.js    which parameters to show, depending on the backend / file / other values
    tar.js            minimal tar reader (ustar / PAX / GNU long names)
    vendor/           pinned third-party libs (no CDN, works offline)
      fzstd-0.1.1.min.js     zstd decompression (MIT)
      marked-15.0.12.min.js  Markdown -> HTML (MIT)
      purify-3.2.6.min.js    DOMPurify, HTML sanitizer (Apache-2.0 / MPL-2.0)
```

## How it works

1. Loads `../openapi.json` and `backends.json`.
2. Keeps every operation whose request body is `application/octet-stream` and whose `200`
   response is `application/zstd` (the `.../process/download` routes).
3. Lists them in a single *Processing* selector, grouped by route:
   - `/{version}/process/download` → one choice per backend of `backend_config.toml`;
   - a literal route (`/auto/process/download`, `/alias/foil/spreadsheet_ocr/process/download`)
     → one choice, grouped by its first path segment.
4. Builds the form from the chosen operation's query/header parameters:
   - `boolean` → checkbox, `enum` → select, `integer`/`number` → number input, other → text;
   - `X-Filename` header → prefilled with the selected file name, sent URL-encoded;
   - `Content-Type` header → `application/octet-stream`.
5. Sends the file, decompresses the `tar.zst` response in the browser and renders
   `content.md` (sanitized, image references rewritten to the archive's images),
   `metadata.json`, the image gallery and `images_error.txt` if present. For `/auto/*` the
   resolved backend (`X-Resolved-Backend` header) is shown. The archive is kept in memory for
   the *Download .tar.zst* button.

Last selected processing and form values are remembered (`localStorage`, key `markgate.ui.form`).

## Parameter visibility (`static/param-rules.js`)

A parameter listed in `param-rules.js` is shown — and sent — only when its function returns
`true`; other parameters are always shown. Example:

```js
// Only foil backends use it, and only for spreadsheet files
spreadsheet_mode: (ui) =>
  ui.backend.backend_type === "foil" && ui.fileHasExtension(["xls", "xlsx", "ods"]),
```

The function receives `ui.backend` (entry of `backends.json` for the chosen backend),
`ui.value(name)`, `ui.isShown(name)`, `ui.fileHasExtension(list)` and `ui.file`.
When a new per-request parameter is added to the API, it appears automatically; add a rule here
only if it should be hidden in some cases.

## `GET /ui/backends.json`

Why it exists: the OpenAPI document only lists backend names, but the rules need each backend's
type and configured defaults. `backend_config.toml` itself is not exposed (upstream URLs, headers,
API keys): this endpoint returns an allow-list only — `backend_type` and the configured value of
each per-request overridable parameter (`SpreadsheetOverrides` fields).

```json
{"foil": {"backend_type": "foil", "defaults": {"spreadsheet_mode": "auto", "excel_min_output_ratio": null}},
 "docling": {"backend_type": "docling", "defaults": {}}}
```

## API keys

If an operation uses HTTP Bearer security, the *API keys* panel lets you set a global key and
per-scope overrides (scoped key, else global key). Scopes: the backend name; for a literal route,
a backend name found in its path (`/alias/foil/...` → `foil`, as aliases share their backend's
key), otherwise the route's first segment (`/auto/...` → `auto`).
Keys live in `sessionStorage` by default, or `localStorage` (`markgate.apiKeys`, unencrypted)
when *Remember keys* is checked.

## Third-party libraries (`static/vendor/`)

Unmodified copies of the official npm distribution builds — not code written for MarkGate.
They are committed rather than declared in a `package.json` or loaded from a CDN,
the same approach `fastapi_offline` takes for Swagger UI: works offline, no third-party host sees
UI visits, no Node toolchain or build step. MIT / Apache-2.0 require shipping each license with
the copy, hence the `LICENSE-*` files.

| File                    | Source (npm tarball → path)                      | sha256                                                             |
|-------------------------|--------------------------------------------------|--------------------------------------------------------------------|
| `fzstd-0.1.1.min.js`    | `fzstd@0.1.1` → `package/umd/index.js`           | `fbea1c25c4413b620ca3f59e6037d9f6c675c5c7f690e4d660143188f686461d` |
| `marked-15.0.12.min.js` | `marked@15.0.12` → `package/marked.min.js`       | `3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c` |
| `purify-3.2.6.min.js`   | `dompurify@3.2.6` → `package/dist/purify.min.js` | `89e1fa7647cb495370d3a997ace4387f5d15d9f4c5af12352c53daa400956287` |

Verify a copy against npm:

    curl -sL https://registry.npmjs.org/marked/-/marked-15.0.12.tgz | tar -xzO package/marked.min.js | sha256sum

To update one: extract the new version's file (and `LICENSE`) the same way, save it as
`<name>-<version>.min.js` / `LICENSE-<name>`, update the `<script>` tag in `index.html`, this table
and the file list above.

## Serving it elsewhere

`static/` can be served by any web server. The UI resolves the API relative to its own location
(`../openapi.json`, `../md/...`) and `backends.json` next to `index.html`: serve it under `/ui/`
behind a reverse proxy that also forwards the API, or change `OPENAPI_URL` / `BACKENDS_URL` /
`API_BASE` at the top of `app.js` and enable CORS on MarkGate. Without `backends.json`,
backend-dependent parameters stay hidden.
