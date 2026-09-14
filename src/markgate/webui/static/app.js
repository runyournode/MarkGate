// MarkGate WebUI — driven by the server's OpenAPI document, no hardcoded routes.
//
// - Discovers "conversion" operations: raw octet-stream request body + application/zstd response.
// - Lists them in one "Processing" selector: an operation with an enum path parameter
//   (e.g. /{version}/process/download) yields one choice per enum value; any other operation
//   (static alias, /auto/...) yields a single choice.
// - Builds the form from the chosen operation's parameters (query / header).
// - Hides parameters according to param-rules.js; hidden parameters are not sent.
// - Decodes the tar.zst response in the browser to render Markdown, images and metadata.
(function () {
  "use strict";

  const OPENAPI_URL = "../openapi.json";
  const BACKENDS_URL = "backends.json";
  const API_BASE = new URL("..", window.location.href);
  const KEYS_STORAGE = "markgate.apiKeys";
  const FORM_STORAGE = "markgate.ui.form";
  const REQUEST_MEDIA_TYPE = "application/octet-stream";
  const RESPONSE_MEDIA_TYPE = "application/zstd";
  const HTTP_METHODS = ["get", "put", "post", "patch", "delete"];
  const ROUTE_SUFFIX = /\/process\/download$/;
  const IMAGE_MIME = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
    webp: "image/webp", bmp: "image/bmp", svg: "image/svg+xml", tif: "image/tiff", tiff: "image/tiff",
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    spec: null,
    backends: {},        // /ui/backends.json: { version: { backend_type, defaults } }
    choices: [],         // [{ id, group, label, operation, pathValues, keyScope }]
    choice: null,
    params: [],          // form parameters of the current operation (path params excluded)
    keyScopes: [],       // API key scopes offered in the keys panel
    hasBearer: false,
    file: null,
    result: null,        // { blob, filename, objectUrls, markdown }
  };

  // ---------------------------------------------------------------------------
  // Storage helpers (never throw)
  // ---------------------------------------------------------------------------

  function storageGet(storage, key) {
    try { return JSON.parse(storage.getItem(key) || "null"); } catch { return null; }
  }
  function storageSet(storage, key, value) {
    try { storage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
  }
  function storageRemove(storage, key) {
    try { storage.removeItem(key); } catch { /* ignore */ }
  }

  // ---------------------------------------------------------------------------
  // OpenAPI helpers
  // ---------------------------------------------------------------------------

  function resolveRef(obj) {
    let current = obj;
    const seen = new Set();
    while (current && current.$ref && !seen.has(current.$ref)) {
      seen.add(current.$ref);
      current = current.$ref
        .replace(/^#\//, "")
        .split("/")
        .reduce((acc, part) => (acc ? acc[part.replace(/~1/g, "/").replace(/~0/g, "~")] : undefined), state.spec);
    }
    return current || {};
  }

  // Flatten $ref / anyOf[x, null] / allOf[x] into a single schema object (outer keys win).
  function resolveSchema(schema) {
    let s = resolveRef(schema || {});
    const variants = s.anyOf || s.oneOf;
    if (variants) {
      const nonNull = variants.map(resolveRef).filter((v) => v.type !== "null");
      if (nonNull.length === 1) s = { ...nonNull[0], ...s, anyOf: undefined, oneOf: undefined };
    }
    if (s.allOf && s.allOf.length === 1) s = { ...resolveRef(s.allOf[0]), ...s, allOf: undefined };
    return s;
  }

  function discoverOperations(spec) {
    const ops = [];
    for (const [path, item] of Object.entries(spec.paths || {})) {
      for (const method of HTTP_METHODS) {
        const op = item[method];
        if (!op) continue;
        const body = resolveRef(op.requestBody || {});
        const ok = resolveRef((op.responses || {})["200"] || {});
        if (body.content && REQUEST_MEDIA_TYPE in body.content && ok.content && RESPONSE_MEDIA_TYPE in ok.content) {
          const params = [...(item.parameters || []), ...(op.parameters || [])].map((p) => {
            const param = resolveRef(p);
            return { ...param, schema: resolveSchema(param.schema) };
          });
          ops.push({ id: `${method.toUpperCase()} ${path}`, method: method.toUpperCase(), path, op, params });
        }
      }
    }
    return ops;
  }

  // One choice per enum value of the (single) enum path param, or one choice for a literal route.
  // API key scope: the backend name; for a literal route, a backend name found in its path
  // (/alias/foil/... uses foil's key), else the route itself (/auto/... uses the "auto" key).
  function buildChoices(operations) {
    const choices = [];
    const backendNames = new Set(
      operations.flatMap((o) => o.params.filter((p) => p.in === "path").flatMap((p) => p.schema.enum || []).map(String))
    );
    for (const operation of operations) {
      const pathParams = operation.params.filter((p) => p.in === "path");
      const enumParam = pathParams.find((p) => Array.isArray(p.schema.enum));
      const routeName = operation.path.replace(ROUTE_SUFFIX, "").replace(/^\/+/, "");

      if (enumParam && pathParams.length === 1) {
        const group = routeName.split("/").filter((s) => !s.startsWith("{")).join("/") || enumParam.name;
        for (const value of enumParam.schema.enum) {
          choices.push({
            id: `${operation.id}#${value}`,
            group: group === enumParam.name ? `${enumParam.name}s` : group,
            label: String(value),
            operation,
            pathValues: { [enumParam.name]: value },
            backend: String(value),
            keyScope: String(value),
          });
        }
      } else if (pathParams.length === 0) {
        const segments = routeName.split("/");
        choices.push({
          id: operation.id,
          group: segments[0],
          label: segments.length > 1 ? segments.slice(1).join(" / ") : segments[0],
          operation,
          pathValues: {},
          backend: null,
          keyScope: segments.find((s) => backendNames.has(s)) || segments[0],
        });
      }
    }
    // Parameterized routes (one choice per backend) first, then literal routes (auto, aliases...)
    const rank = (c) => (Object.keys(c.pathValues).length ? 0 : 1);
    return choices.sort((a, b) => rank(a) - rank(b));
  }

  function hasBearerSecurity(spec, op) {
    const schemes = (spec.components || {}).securitySchemes || {};
    const requirements = op.security || spec.security || [];
    return requirements.some((req) =>
      Object.keys(req).some((name) => {
        const s = resolveRef(schemes[name] || {});
        return s.type === "http" && String(s.scheme).toLowerCase() === "bearer";
      })
    );
  }

  // ---------------------------------------------------------------------------
  // API keys
  // ---------------------------------------------------------------------------

  const keys = {
    data: { remember: false, global: "", scoped: {} },

    load() {
      const persisted = storageGet(localStorage, KEYS_STORAGE);
      const session = storageGet(sessionStorage, KEYS_STORAGE);
      const loaded = persisted || session;
      if (loaded) this.data = { remember: !!persisted, global: loaded.global || "", scoped: loaded.scoped || {} };
    },

    save() {
      const payload = { global: this.data.global, scoped: this.data.scoped };
      if (this.data.remember) {
        storageSet(localStorage, KEYS_STORAGE, payload);
        storageRemove(sessionStorage, KEYS_STORAGE);
      } else {
        storageSet(sessionStorage, KEYS_STORAGE, payload);
        storageRemove(localStorage, KEYS_STORAGE);
      }
      renderKeysStatus();
    },

    clear() {
      this.data = { remember: this.data.remember, global: "", scoped: {} };
      storageRemove(localStorage, KEYS_STORAGE);
      storageRemove(sessionStorage, KEYS_STORAGE);
    },

    resolve(scope) {
      return (scope && this.data.scoped[scope]) || this.data.global || "";
    },
  };

  function secretInput(input) {
    const toggle = input.parentElement.querySelector(".toggle-secret");
    toggle.addEventListener("click", () => {
      input.type = input.type === "password" ? "text" : "password";
    });
  }

  function renderKeys() {
    $("#keys-card").hidden = !state.hasBearer;
    $("#key-global").value = keys.data.global;
    $("#keys-remember").checked = keys.data.remember;

    const container = $("#keys-scoped");
    container.replaceChildren();
    if (state.keyScopes.length) {
      const title = document.createElement("div");
      title.className = "scoped-keys-title";
      title.textContent = "Per processing (overrides the global key)";
      container.append(title);
    }
    for (const scope of state.keyScopes) {
      const field = document.createElement("div");
      field.className = "field";
      field.innerHTML = `<label></label><div class="secret"><input type="password"><button type="button" class="ghost toggle-secret" aria-label="Show key">👁</button></div>`;
      const label = field.querySelector("label");
      const input = field.querySelector("input");
      label.textContent = scope;
      input.id = `key-scoped-${scope}`;
      label.htmlFor = input.id;
      input.placeholder = "(global key)";
      input.value = keys.data.scoped[scope] || "";
      input.addEventListener("input", () => {
        if (input.value) keys.data.scoped[scope] = input.value;
        else delete keys.data.scoped[scope];
        keys.save();
      });
      container.append(field);
      secretInput(input);
    }
    renderKeysStatus();
  }

  function renderKeysStatus() {
    if (!state.hasBearer || !state.choice) return;
    const scope = state.choice.keyScope;
    const status = $("#keys-status");
    if (!keys.resolve(scope)) {
      status.textContent = "— no key set";
      $("#keys-details").open = true;
    } else if (keys.data.scoped[scope]) {
      status.textContent = `— using key for “${scope}”`;
    } else {
      status.textContent = "— using global key";
    }
  }

  function initKeysPanel() {
    const globalInput = $("#key-global");
    secretInput(globalInput);
    globalInput.addEventListener("input", () => {
      keys.data.global = globalInput.value;
      keys.save();
    });
    $("#keys-remember").addEventListener("change", (e) => {
      keys.data.remember = e.target.checked;
      keys.save();
    });
    $("#keys-clear").addEventListener("click", () => {
      if (!confirm("Clear all stored API keys?")) return;
      keys.clear();
      renderKeys();
    });
  }

  // ---------------------------------------------------------------------------
  // Dynamic form
  // ---------------------------------------------------------------------------

  const isFilenameHeader = (p) => p.in === "header" && p.name.toLowerCase() === "x-filename";
  const isContentTypeHeader = (p) => p.in === "header" && p.name.toLowerCase() === "content-type";

  function savedForm() {
    return storageGet(localStorage, FORM_STORAGE) || {};
  }

  function persistForm() {
    const values = currentValues(false);
    const toSave = {};
    for (const p of state.params) {
      if (isFilenameHeader(p) || !(p.name in values)) continue;
      toSave[p.name] = values[p.name];
    }
    storageSet(localStorage, FORM_STORAGE, {
      ...savedForm(),
      _choice: state.choice.id,
      [state.choice.operation.id]: toSave,
    });
  }

  function defaultValue(p, saved) {
    if (saved && p.name in saved) return saved[p.name];
    if (p.schema.default !== undefined && p.schema.default !== null) return p.schema.default;
    if (isContentTypeHeader(p)) return REQUEST_MEDIA_TYPE;
    if (p.schema.enum && p.schema.enum.length && p.required) return p.schema.enum[0];
    return p.schema.type === "boolean" ? false : "";
  }

  function buildWidget(p, value) {
    const s = p.schema;
    const type = Array.isArray(s.type) ? s.type.find((t) => t !== "null") : s.type;
    let input;
    if (s.enum) {
      input = document.createElement("select");
      if (!p.required) input.append(new Option("(default)", ""));
      for (const option of s.enum) input.append(new Option(String(option), String(option)));
      input.value = value == null ? "" : String(value);
    } else if (type === "boolean") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = value === true || value === "true";
    } else {
      input = document.createElement("input");
      input.type = type === "integer" || type === "number" ? "number" : "text";
      if (type === "integer") input.step = "1";
      else if (type === "number") input.step = "any";
      if (s.minimum !== undefined) input.min = s.minimum;
      if (s.maximum !== undefined) input.max = s.maximum;
      input.value = value == null ? "" : String(value);
      const example = (s.examples && s.examples[0]) ?? p.example;
      if (example !== undefined) {
        // X-Filename examples are URL-encoded, but the field holds the readable name
        let placeholder = String(example);
        if (isFilenameHeader(p)) { try { placeholder = decodeURIComponent(placeholder); } catch { /* keep raw */ } }
        input.placeholder = placeholder;
      } else if (!p.required && type !== "boolean") {
        input.placeholder = "(default)";
      }
    }
    input.dataset.param = p.name;
    input.dataset.kind = type || "string";
    return input;
  }

  function renderForm() {
    const container = $("#params");
    container.replaceChildren();
    const saved = savedForm()[state.choice.operation.id];

    const order = { query: 0, header: 1, cookie: 2 };
    const params = [...state.params].sort((a, b) => (order[a.in] ?? 9) - (order[b.in] ?? 9));

    for (const p of params) {
      const field = document.createElement("div");
      field.className = "field";
      field.dataset.field = p.name;

      const value = isFilenameHeader(p) ? (state.file ? state.file.name : "") : defaultValue(p, saved);
      const input = buildWidget(p, value);
      input.id = `param-${p.name}`;
      const title = p.schema.title || p.name;

      if (input.type === "checkbox") {
        const wrap = document.createElement("label");
        wrap.className = "check";
        wrap.append(input, document.createTextNode(" " + title));
        field.append(wrap);
      } else {
        const label = document.createElement("label");
        label.htmlFor = input.id;
        label.textContent = title + (p.required ? " *" : "");
        field.append(label, input);
      }

      const hintText = isFilenameHeader(p)
        ? "Defaults to the selected file name (sent URL-encoded)."
        : (p.description || p.schema.description || "");
      if (hintText) {
        const hint = document.createElement("div");
        hint.className = "hint";
        hint.textContent = hintText.replace(/\*\*/g, "");
        field.append(hint);
      }

      const onChange = () => {
        applyVisibility();
        persistForm();
        updateSubmit();
      };
      input.addEventListener("input", onChange);
      input.addEventListener("change", onChange);
      container.append(field);
    }
    applyVisibility();
    updateSubmit();
  }

  function readInput(input) {
    if (input.type === "checkbox") return input.checked;
    if (input.dataset.kind === "integer" || input.dataset.kind === "number") {
      return input.value === "" ? "" : Number(input.value);
    }
    return input.value;
  }

  // Returns {name: value} for path params (from the choice) and form inputs;
  // when onlyVisible, hidden parameters are omitted.
  function currentValues(onlyVisible = true) {
    const values = { ...state.choice.pathValues };
    for (const input of $$("#params [data-param]")) {
      const field = input.closest("[data-field]");
      if (onlyVisible && field.hidden) continue;
      values[input.dataset.param] = readInput(input);
    }
    return values;
  }

  // Shows/hides form parameters with the functions of param-rules.js (window.MarkgateParamRules).
  // A parameter missing from the current form counts as hidden; a rule that throws shows its parameter.
  function applyVisibility() {
    const rules = window.MarkgateParamRules || {};
    const all = currentValues(false);
    const memo = {};
    const extension = () => {
      const dot = state.file ? state.file.name.lastIndexOf(".") : -1;
      return dot > 0 ? state.file.name.slice(dot + 1).toLowerCase() : "";
    };

    const isShown = (name, stack = new Set()) => {
      if (name in memo) return memo[name];
      if (!(name in all)) return false;
      const rule = rules[name];
      if (typeof rule !== "function" || stack.has(name)) return (memo[name] = true);
      stack.add(name);
      let shown = true;
      try {
        shown = !!rule({
          backend: { defaults: {}, ...(state.backends[state.choice.backend] || {}) },
          value: (other) => (all[other] == null ? "" : all[other]),
          isShown: (other) => isShown(other, stack),
          fileHasExtension: (list) => list.map((e) => e.toLowerCase()).includes(extension()),
          file: state.file,
        });
      } catch (err) {
        console.warn(`param-rules.js: rule "${name}" failed`, err);
      }
      stack.delete(name);
      return (memo[name] = shown);
    };

    for (const field of $$("#params [data-field]")) {
      field.hidden = !isShown(field.dataset.field);
    }
  }

  function updateSubmit() {
    const values = currentValues();
    const missing = state.params.some((p) =>
      p.required && p.name in values && p.schema.type !== "boolean" && (values[p.name] === "" || values[p.name] == null)
    );
    $("#submit").disabled = !state.file || missing;
  }

  // ---------------------------------------------------------------------------
  // File selection
  // ---------------------------------------------------------------------------

  function setFile(file) {
    if (!file) return;
    state.file = file;
    $("#file-drop").classList.add("has-file");
    $("#file-label").textContent = `${file.name} (${formatBytes(file.size)})`;
    for (const p of state.params.filter(isFilenameHeader)) {
      const input = $(`#param-${CSS.escape(p.name)}`);
      if (input) input.value = file.name;
    }
    applyVisibility();
    updateSubmit();
  }

  function initFilePicker() {
    const drop = $("#file-drop");
    const input = $("#file-input");
    drop.addEventListener("click", () => input.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", () => setFile(input.files[0]));
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("over");
      setFile(e.dataTransfer.files[0]);
    });
  }

  // ---------------------------------------------------------------------------
  // Request
  // ---------------------------------------------------------------------------

  function buildRequest() {
    const values = currentValues();
    const { operation } = state.choice;
    let path = operation.path;
    const query = new URLSearchParams();
    const headers = {};

    for (const [name, value] of Object.entries(state.choice.pathValues)) {
      path = path.replace(`{${name}}`, encodeURIComponent(value));
    }
    for (const p of state.params) {
      if (!(p.name in values)) continue; // hidden
      const value = values[p.name];
      if (value === "" || value == null) continue;
      if (p.in === "query") {
        if (!p.required && p.schema.default !== undefined && value === p.schema.default) continue;
        query.append(p.name, String(value));
      } else if (p.in === "header") {
        headers[p.name] = isFilenameHeader(p) ? encodeURIComponent(value) : String(value);
      }
    }

    const key = state.hasBearer ? keys.resolve(state.choice.keyScope) : "";
    if (key) headers["Authorization"] = `Bearer ${key}`;
    if (!Object.keys(headers).some((h) => h.toLowerCase() === "content-type")) {
      headers["Content-Type"] = REQUEST_MEDIA_TYPE;
    }

    const url = new URL(path.replace(/^\//, ""), API_BASE);
    url.search = query.toString();
    return { url, headers, method: operation.method };
  }

  async function submit(event) {
    event.preventDefault();
    if (!state.file) return;
    const { url, headers, method } = buildRequest();
    const choice = state.choice;

    showState("busy");
    $("#submit").disabled = true;
    const started = performance.now();
    try {
      const response = await fetch(url, { method, headers, body: state.file });
      if (!response.ok) throw new Error(await describeHttpError(response));
      const blob = await response.blob();
      const elapsed = performance.now() - started;
      const files = MarkgateTar.parse(fzstd.decompress(new Uint8Array(await blob.arrayBuffer())));
      const resolved = response.headers.get("X-Resolved-Backend");
      renderResult({
        blob,
        files,
        elapsed,
        filename: filenameFromResponse(response) || `${state.file.name}.tar.zst`,
        label: `${choice.group}: ${choice.label}${resolved ? ` → ${resolved}` : ""}`,
        query: url.search,
      });
    } catch (err) {
      showState("error", err.message || String(err));
    } finally {
      updateSubmit();
    }
  }

  async function describeHttpError(response) {
    let detail = "";
    try {
      const text = await response.text();
      try {
        const json = JSON.parse(text);
        detail = typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail ?? json, null, 2);
      } catch {
        detail = text;
      }
    } catch { /* ignore */ }
    return `HTTP ${response.status} ${response.statusText}${detail ? "\n" + detail : ""}`;
  }

  function filenameFromResponse(response) {
    const cd = response.headers.get("Content-Disposition") || "";
    const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
    if (star) { try { return decodeURIComponent(star[1]); } catch { /* ignore */ } }
    const plain = cd.match(/filename="?([^";]+)"?/i);
    return plain ? plain[1] : null;
  }

  // ---------------------------------------------------------------------------
  // Result rendering
  // ---------------------------------------------------------------------------

  function showState(which, message) {
    $("#result-empty").hidden = which !== "empty";
    $("#result-busy").hidden = which !== "busy";
    $("#result").hidden = which !== "result";
    const error = $("#result-error");
    error.hidden = which !== "error";
    if (which === "error") error.textContent = message;
  }

  const normalizePath = (p) => {
    let path = p.trim();
    try { path = decodeURI(path); } catch { /* keep raw */ }
    return path.replace(/^(\.\/)+/, "").replace(/^\/+/, "");
  };

  function renderResult({ blob, files, elapsed, filename, label, query }) {
    if (state.result) state.result.objectUrls.forEach((u) => URL.revokeObjectURL(u));
    const utf8 = new TextDecoder("utf-8");
    const objectUrls = [];
    const images = new Map();
    let markdown = "";
    let metadata = null;
    let imagesError = null;

    for (const f of files) {
      const name = normalizePath(f.name);
      if (name === "content.md") markdown = utf8.decode(f.data);
      else if (name === "metadata.json") metadata = utf8.decode(f.data);
      else if (name === "images_error.txt") imagesError = utf8.decode(f.data);
      else {
        const ext = name.split(".").pop().toLowerCase();
        const url = URL.createObjectURL(new Blob([f.data], { type: IMAGE_MIME[ext] || "application/octet-stream" }));
        objectUrls.push(url);
        images.set(name, url);
      }
    }
    state.result = { blob, filename, objectUrls, markdown };

    $("#result-info").textContent =
      `${label}${query ? ` (${decodeURIComponent(query)})` : ""} · ${state.file.name} · ${(elapsed / 1000).toFixed(1)} s · ` +
      `${markdown.length.toLocaleString()} chars · ${images.size} image(s) · archive ${formatBytes(blob.size)}`;

    const imgErr = $("#images-error");
    imgErr.hidden = !imagesError;
    imgErr.textContent = imagesError || "";

    // Preview: sanitize, then rewrite image references inside an inert template before inserting.
    const template = document.createElement("template");
    template.innerHTML = DOMPurify.sanitize(marked.parse(markdown));
    for (const img of template.content.querySelectorAll("img")) {
      const src = img.getAttribute("src") || "";
      const url = images.get(normalizePath(src));
      if (url) img.src = url;
      else if (!/^(https?:|data:)/i.test(src)) {
        img.removeAttribute("src");
        img.classList.add("missing");
        img.alt = `[missing image: ${src}] ${img.alt || ""}`;
      }
      img.loading = "lazy";
    }
    for (const a of template.content.querySelectorAll("a[href]")) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    $('[data-panel="preview"]').replaceChildren(template.content);

    $("#md-raw").textContent = markdown;
    let metaText = metadata ?? "(no metadata)";
    try { metaText = JSON.stringify(JSON.parse(metadata), null, 2); } catch { /* keep raw */ }
    $("#md-meta").textContent = metaText;

    const gallery = $('[data-panel="images"]');
    gallery.replaceChildren();
    for (const [name, url] of images) {
      const figure = document.createElement("figure");
      const img = document.createElement("img");
      img.src = url;
      img.alt = name;
      img.loading = "lazy";
      img.addEventListener("click", () => window.open(url, "_blank"));
      const caption = document.createElement("figcaption");
      caption.textContent = name;
      figure.append(img, caption);
      gallery.append(figure);
    }
    $("#images-count").textContent = images.size ? `(${images.size})` : "";

    showState("result");
  }

  function initResultActions() {
    for (const tab of $$(".tabs [data-tab]")) {
      tab.addEventListener("click", () => {
        for (const t of $$(".tabs [data-tab]")) t.classList.toggle("active", t === tab);
        for (const panel of $$("[data-panel]")) panel.hidden = panel.dataset.panel !== tab.dataset.tab;
      });
    }
    $("#download").addEventListener("click", () => {
      if (!state.result) return;
      const url = URL.createObjectURL(state.result.blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = state.result.filename;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    $("#copy-md").addEventListener("click", async (e) => {
      if (!state.result) return;
      const button = e.currentTarget;
      try {
        await navigator.clipboard.writeText(state.result.markdown);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Copy failed";
      }
      setTimeout(() => { button.textContent = "Copy Markdown"; }, 1500);
    });
  }

  // ---------------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------------

  function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 ** 2).toFixed(1)} MB`;
  }

  function selectChoice(id) {
    const previous = state.choice;
    state.choice = state.choices.find((c) => c.id === id) || state.choices[0];
    state.params = state.choice.operation.params.filter((p) => p.in !== "path");
    state.hasBearer = hasBearerSecurity(state.spec, state.choice.operation.op);
    const op = state.choice.operation.op;
    $("#operation-description").textContent = (op.description || op.summary || "").replace(/`/g, "");
    if (!previous || previous.operation !== state.choice.operation) renderForm();
    else { applyVisibility(); updateSubmit(); }
    renderKeysStatus();
    persistForm();
  }

  function renderChoices() {
    const select = $("#processing-select");
    select.replaceChildren();
    const groups = new Map();
    for (const choice of state.choices) {
      if (!groups.has(choice.group)) {
        const optgroup = document.createElement("optgroup");
        optgroup.label = choice.group;
        groups.set(choice.group, optgroup);
        select.append(optgroup);
      }
      groups.get(choice.group).append(new Option(choice.label, choice.id));
    }
    const saved = savedForm()._choice;
    if (state.choices.some((c) => c.id === saved)) select.value = saved;
    select.addEventListener("change", () => selectChoice(select.value));
    return select.value;
  }

  async function init() {
    initKeysPanel();
    initFilePicker();
    initResultActions();
    $("#process-form").addEventListener("submit", submit);
    keys.load();

    const fail = (message) => {
      const box = $("#load-error");
      box.hidden = false;
      box.textContent = message;
    };

    // backends.json is optional: without it, rules depending on the backend just hide their params
    const backendsRequest = fetch(BACKENDS_URL)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .catch((err) => {
        console.warn(`Cannot load ${BACKENDS_URL}, backend-specific parameters stay hidden`, err);
        return {};
      });
    try {
      const response = await fetch(OPENAPI_URL);
      if (!response.ok) throw new Error(`Cannot load ${OPENAPI_URL}: HTTP ${response.status}`);
      state.spec = await response.json();
    } catch (err) {
      fail(err.message);
      return;
    }
    state.backends = await backendsRequest;

    state.choices = buildChoices(discoverOperations(state.spec));
    if (!state.choices.length) {
      fail(`No endpoint accepting ${REQUEST_MEDIA_TYPE} and returning ${RESPONSE_MEDIA_TYPE} found in the OpenAPI document.`);
      return;
    }
    state.keyScopes = [...new Set(state.choices.map((c) => c.keyScope))];
    state.hasBearer = state.choices.some((c) => hasBearerSecurity(state.spec, c.operation.op));
    renderKeys();
    selectChoice(renderChoices());
  }

  init();
})();
