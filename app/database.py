"""Database engine/session setup (SQLite by default)."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()


def _normalise_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+psycopg://"):
        return url
    # Managed Postgres providers (Supabase, Neon, etc.) append query params that are NOT valid
    # libpq connection keywords — e.g. Supabase's `supa=...` and Prisma-style `pgbouncer=true` —
    # and psycopg rejects them with 'invalid connection option'. Keep only real libpq keywords
    # and ensure TLS (hosted Postgres requires SSL).
    from urllib.parse import parse_qsl, urlencode
    _LIBPQ = {"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name",
              "options", "target_session_attrs", "hostaddr", "keepalives", "keepalives_idle",
              "gssencmode", "channel_binding"}
    base, _, query = url.partition("?")
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.lower() in _LIBPQ]
    if not any(k.lower() == "sslmode" for k, _ in params):
        params.append(("sslmode", "require"))
    return base + "?" + urlencode(params)


# Resolve the effective URL. When DATABASE_URL is left at the SQLite default but a platform
# Postgres connection string is present (e.g. Vercel Postgres / Neon set POSTGRES_URL), use it —
# so a Vercel deployment with a linked Postgres works with zero extra configuration.
_raw_url = settings.database_url
if _raw_url.startswith("sqlite"):
    _platform_pg = os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_NON_POOLING")
    if _platform_pg:
        _raw_url = _platform_pg
    elif os.getenv("VERCEL"):
        # On Vercel with no managed Postgres linked, the project filesystem is read-only — a
        # relative SQLite path would crash the function at startup. Fall back to a writable (but
        # EPHEMERAL) /tmp database so the app still boots; link Vercel Postgres for durable data.
        _raw_url = "sqlite:////tmp/accounting.sqlite3"

DATABASE_URL = _normalise_db_url(_raw_url)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if db_path and db_path != ":memory:":
        # Best-effort: on a read-only serverless filesystem this dir can't be created; that's
        # fine because a real deployment uses Postgres (above), not this SQLite fallback.
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        except OSError:
            pass

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_columns(table: str, columns: dict[str, str]) -> None:
    """Lightweight additive migration: ADD COLUMN for any missing columns, preserving data.
    Bridges create_all for new columns on pre-existing tables (SQLite has no ALTER-to-add
    with all features, but ADD COLUMN works). No-op if the table or column already matches."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    with engine.begin() as conn:
        for name, ddl_type in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
