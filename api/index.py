"""Vercel Python serverless entry point.

WARNING — READ DEPLOY.md: Vercel runs this as a stateless serverless function on a read-only
filesystem (only /tmp is writable, and it is wiped between invocations). The app's default
SQLite database therefore does NOT persist on Vercel — every cold start resets the data. This
entry point is for a throwaway DEMO only. For a real deployment use the Docker/persistent-disk
path (Render/Railway/Fly) in DEPLOY.md, or point DATABASE_URL at an external managed Postgres.
"""

from app.main import app  # noqa: F401  — Vercel's @vercel/python serves this ASGI `app`
