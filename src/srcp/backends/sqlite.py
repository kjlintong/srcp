"""SQLite backend. Uses BEGIN IMMEDIATE for a real cross-process write lock."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .base import Backend, State


class SQLiteBackend(Backend):
    name: str = "sqlite"

    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.path = path
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS srcp_state (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[State]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT v FROM srcp_state WHERE k = 'state'").fetchone()
            state = State.from_dict(json.loads(row[0])) if row else State()
            try:
                yield state
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO srcp_state (k, v) VALUES ('state', ?)",
                    (json.dumps(state.to_dict(), ensure_ascii=False),),
                )
                connection.execute("COMMIT")
        finally:
            connection.close()
