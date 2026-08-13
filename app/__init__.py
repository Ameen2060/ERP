"""Standalone double-entry accounting application.

A single FastAPI process that serves both the JSON API and a built-in web UI. Entirely
self-contained: SQLite storage, no external services, no Node build step.
"""

__version__ = "0.1.0"
