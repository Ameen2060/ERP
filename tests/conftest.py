"""Point the app at a throwaway SQLite DB before the app modules build their engine."""

from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="acc-test-")
_db_path = os.path.join(_tmp, "test.sqlite3").replace(os.sep, "/")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_db_path}")
os.environ.setdefault("SEED_ON_STARTUP", "true")
os.environ.setdefault("AUTH_ENABLED", "false")  # keep the general suite open; auth tested separately
