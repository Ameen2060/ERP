"""Convenience launcher: `python run.py` starts the app on the configured port (8100)."""

from __future__ import annotations

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port, reload=False)
