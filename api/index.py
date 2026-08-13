"""Vercel Python serverless entry point (ASGI).

The app runs on Vercel's Python runtime. Because that runtime does not reliably fire ASGI
lifespan events, we initialize the database (create tables + seed) here at cold start.

REQUIREMENTS for a working Vercel deployment (see DEPLOY.md):
  * A hosted Postgres database — add **Vercel Postgres** (Storage tab) or a Neon database. It
    sets POSTGRES_URL, which this app auto-detects. SQLite does NOT persist on Vercel.
  * Durable file storage — add a **Vercel Blob** store (Storage tab). It sets
    BLOB_READ_WRITE_TOKEN, which this app auto-detects; all uploaded documents, archived exports
    and the org logo are then stored durably in Blob (survive redeploys/restarts/new devices).
  * Env vars: SECRET_KEY, ADMIN_PASSWORD, AUTH_ENABLED=true, RESET_EXPOSE_TOKEN=false.
"""

from app.main import app, ensure_bootstrap  # noqa: F401  — Vercel serves the ASGI `app`

# Initialize schema + seed on cold start (idempotent). ensure_bootstrap() never raises — on a DB
# error it records a scrubbed message surfaced at GET /health and retries on the next request, so
# a misconfigured database degrades gracefully instead of crashing the whole function.
ensure_bootstrap()
