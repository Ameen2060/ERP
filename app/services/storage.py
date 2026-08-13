"""Pluggable durable file storage for all uploaded documents & archived exports.

Every binary the app persists (transaction attachments, archived PDFs/Excels, AI-analysis source
documents, org logo) goes through this layer. The backend is chosen at runtime:

  * **Vercel Blob** — used automatically when ``BLOB_READ_WRITE_TOKEN`` is present (i.e. a Vercel
    Blob store is linked). Files live in durable object storage and survive redeploys, restarts,
    and are reachable from any device. The returned reference is the Blob URL, stored in Postgres.
  * **Filesystem** — the default for local dev and persistent-disk hosts (Render/Railway/Fly with
    a mounted disk). The reference is an absolute path. Behaviour is unchanged from before.

Reads/writes go through ``save`` / ``read`` / ``delete`` so callers never care which backend is
active. Blob objects are given an unguessable random suffix and are only ever fetched
server-side and streamed through the app's authenticated download endpoint — the Blob URL is
never exposed to the browser.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from ..config import get_settings

_BLOB_BASE = "https://blob.vercel-storage.com"


class StorageError(Exception):
    """Raised when a durable-storage operation fails."""


def _blob_token() -> str | None:
    return os.getenv("BLOB_READ_WRITE_TOKEN") or None


def backend_name() -> str:
    return "vercel_blob" if _blob_token() else "filesystem"


def _is_url(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")


# ── Vercel Blob REST client (stdlib only — keeps the serverless bundle small) ─────────────────
def _blob_put(key: str, data: bytes, content_type: str) -> str:
    token = _blob_token()
    api_version = os.getenv("BLOB_API_VERSION", "7")
    url = f"{_BLOB_BASE}/{key.lstrip('/')}"
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("authorization", f"Bearer {token}")
    req.add_header("x-api-version", api_version)
    req.add_header("x-content-type", content_type or "application/octet-stream")
    req.add_header("x-add-random-suffix", "1")  # unguessable object URLs
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # surface a readable error
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise StorageError(f"Vercel Blob upload failed ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise StorageError(f"Vercel Blob upload failed: {e.reason}") from e
    ref = body.get("url")
    if not ref:
        raise StorageError("Vercel Blob upload returned no URL.")
    return ref


def _blob_delete(url: str) -> None:
    token = _blob_token()
    if not token:
        return
    api_version = os.getenv("BLOB_API_VERSION", "7")
    req = urllib.request.Request(f"{_BLOB_BASE}/delete",
                                 data=json.dumps({"urls": [url]}).encode("utf-8"),
                                 method="POST")
    req.add_header("authorization", f"Bearer {token}")
    req.add_header("x-api-version", api_version)
    req.add_header("content-type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass  # best-effort; a failed delete must not break the app flow


def _http_get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise StorageError(f"Could not fetch stored file: {e}") from e


# ── Public API ────────────────────────────────────────────────────────────────────────────────
def save(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Persist ``data`` under a logical ``key`` (e.g. 'sales_invoice/<id>/<att>.pdf'). Returns a
    reference (a Blob URL or an absolute filesystem path) to store in the database."""
    if _blob_token():
        return _blob_put(key, data, content_type)
    base = get_settings().attachments_dir
    # On Vercel with no Blob store linked, the project filesystem is read-only — writing there
    # 500s. Fall back to a writable (but EPHEMERAL) /tmp dir so uploads don't crash; link Vercel
    # Blob for durable file storage.
    if os.getenv("VERCEL") and not os.path.abspath(base).replace("\\", "/").startswith("/tmp"):
        base = "/tmp/attachments"
    path = os.path.join(base, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def read(ref: str) -> bytes:
    if not ref:
        raise StorageError("Empty storage reference.")
    if _is_url(ref):
        return _http_get(ref)
    if not os.path.exists(ref):
        raise StorageError("File is missing from storage.")
    with open(ref, "rb") as fh:
        return fh.read()


def exists(ref: str) -> bool:
    if not ref:
        return False
    if _is_url(ref):
        return True  # the stored URL is authoritative for durable object storage
    return os.path.exists(ref)


def delete(ref: str) -> None:
    if not ref:
        return
    if _is_url(ref):
        _blob_delete(ref)
    else:
        try:
            os.remove(ref)
        except OSError:
            pass
