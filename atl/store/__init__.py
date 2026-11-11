"""SQLite store (lane L3).

One SQLite file shared by the MCP server and the `atl approve` CLI. Models are stored as JSON in `body`
with extra columns for lookups. Connections use WAL, a 5 s busy timeout and a thread lock, and every
write commits immediately, so two processes (or two open_store calls) see each other's writes.

Public API (frozen):
    open_store(db_path: Path) -> Store     # creates the file and applies schema.sql
    reset_store(db_path: Path) -> None     # deletes the database file (used by `make reset` and the demo)
"""

from __future__ import annotations

from pathlib import Path

from atl.contracts import Store
from atl.store.sqlite_store import SQLiteStore

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def open_store(db_path: Path) -> Store:
    return SQLiteStore(Path(db_path), SCHEMA_PATH.read_text(encoding="utf-8"))


def reset_store(db_path: Path) -> None:
    db_path = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
