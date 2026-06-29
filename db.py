"""
Storage backend selector.

By default uses SQLite (storage.py). Set DB_BACKEND=postgres (and DATABASE_URL)
to use the Postgres backend (storage_pg.py), which supports concurrent writers.

Usage in code:
    from db import get_store
    store = get_store()          # picks backend from env
    store = get_store("data.db") # SQLite path (ignored for postgres)
"""

import os


def get_store(sqlite_path=None):
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    if backend in ("postgres", "postgresql", "pg"):
        from storage_pg import Store
        return Store()  # reads DATABASE_URL
    from storage import Store
    return Store(sqlite_path or os.environ.get("PRICE_DB", "prices.db"))
