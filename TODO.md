# TODO / Known issues to revisit

## Image upload to S3 blocks the response, even on `/md/{version}/process`

**Where**: `services.py` — `_resolve_document()` calls `await update_s3_processed(...)` synchronously
on every cache miss, before returning to the caller. `update_s3_processed()` (also `services.py`)
re-encodes every extracted image (PIL → bytes) and uploads it to S3.

**Problem**: This upload is on the critical path of *every* route on a cache miss, including
`PUT /md/{version}/process`, which never returns images to its caller. That client waits for an
S3 write it gets no benefit from this request — the upload only pays off for a *future* request to
`/{version}/process` or `/{version}/process/download` on the same file + cache signature.

Contrast with the read side, which is already lazy and correctly avoids this: on a cache hit,
`_resolve_document()` returns `images={}` without touching S3 at all, and `gather_images()` only
fetches images from S3 when the calling route actually needs them (`/md/...` never calls it). The
write side has no such laziness — it always uploads, and always blocks.

**Why it hasn't been fixed yet**: `update_s3_processed()` runs *inside* the Redis lock
(`get_lock_name`/`_resolve_document`), which is what currently prevents a concurrent request for
the same file + signature from missing the cache and reprocessing while the first request's result
is still being written. Naively moving the image upload to a `background_tasks.add_task(...)` (like
`background_update_s3()` already does for the source file / aliases / hit-count metadata) means
releasing the lock before that upload completes — reopening a race window where a second concurrent
request could cache-miss on images specifically (content.md/metadata.json would still be written
and lock-protected, so no duplicate *upstream* call — only a possible duplicate image upload).

**Possible directions** (not decided, needs its own analysis before touching):
- Accept the race (duplicate image upload is idempotent — same bytes, same key — just wasted work
  on the rare overlap) and background only the image part of `update_s3_processed()`.
- Keep it lock-protected but make it non-blocking for the *response* specifically (fire the coroutine,
  don't await it before returning) — trickier: still need to guarantee it completes even if the
  event loop moves on, and still need it to run before the lock is released.
- Leave as-is: it's extra latency, not extra correctness risk, and only matters for large images /
  many images per document.

Raised during the MarkGate query-params/cache-key work (see git log around
`backends/foil.py`'s `SpreadsheetOverrides` / `ProcessingConfig.cache_key()`).
