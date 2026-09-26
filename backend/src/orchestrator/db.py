"""SQLite persistence with file-based migrations.

The repository layer isolates SQL so the engine can later move to PostgreSQL
by swapping this module's implementation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if hasattr(value, "value") and isinstance(value, __import__("enum").Enum):
        return value.value
    return value


class Database:
    """Thread-safe SQLite handle. One instance per application."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            applied = (
                {r[0] for r in self._conn.execute("SELECT version FROM schema_migrations").fetchall()}
                if self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                else set()
            )
            for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(sql_file.stem.split("_")[0])
                if version in applied:
                    continue
                self._conn.executescript(sql_file.read_text())
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat()),
                )
            self._conn.commit()

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.cursor() as cur:
            cur.execute(sql, tuple(_serialize(p) for p in params))

    def insert(self, table: str, data: dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        values = tuple(_serialize(v) for v in data.values())
        with self.cursor() as cur:
            cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", values)  # noqa: S608 - table name is internal constant

    def update(self, table: str, row_id: str, data: dict[str, Any], key: str = "id") -> None:
        assignments = ", ".join(f"{k} = ?" for k in data)
        values = tuple(_serialize(v) for v in data.values()) + (row_id,)
        with self.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", values)  # noqa: S608

    def get(self, table: str, row_id: str, key: str = "id") -> dict[str, Any] | None:
        with self.cursor() as cur:
            row = cur.execute(f"SELECT * FROM {table} WHERE {key} = ?", (row_id,)).fetchone()  # noqa: S608
            return dict(row) if row else None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            return [dict(r) for r in cur.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
