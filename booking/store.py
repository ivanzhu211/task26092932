"""SQLite 持久化层：时段与预约/候补请求。"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    slot_id INTEGER PRIMARY KEY,
    start   INTEGER NOT NULL,
    "end"   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id    INTEGER NOT NULL REFERENCES slots(slot_id),
    status     TEXT NOT NULL CHECK (status IN ('waitlist','pending','confirmed','cancelled')),
    created_at INTEGER NOT NULL,
    promoted_at INTEGER,
    hold_deadline INTEGER
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()
