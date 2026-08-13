"""Vercel Python serverless entry point (ASGI).

The app runs on Vercel's Python runtime. Because that runtime does not reliably fire ASGI
lifespan events, we initialize the database (create tables + seed) here at cold start.

REQUIREMENTS for a working Vercel deployment (see DEPLOY.md):
  * A hosted Postgres database — add **Vercel Postgres** (Storage tab) or a Neon database and
    link it to the project. It sets POSTGRES_URL, which this app auto-detects. SQLite does NOT
    persist on Vercel (read-only, ephemeral filesystem).
  * Env vars: SECRET_KEY, ADMIN_PASSWORD, AUTH_ENABLED=true, RESET_EXPOSE_TOKEN=false,
    ATTACHMENTS_DIR=/tmp/attachments, ORG_DIR=/tmp/org.
  * Note: uploaded files (attachments/logo) are written under /tmp and are ephemeral on Vercel;
    accounting data in Postgres persists. Durable file storage needs Vercel Blob (future work).
"""

from app.main import app, bootstrap_database  # noqa: F401  — Vercel serves the ASGI `app`

# Initialize schema + seed on cold start (idempotent). If the database is unreachable this will
# raise and Vercel will surface it in the function logs — which is the correct, visible failure.
bootstrap_database()
