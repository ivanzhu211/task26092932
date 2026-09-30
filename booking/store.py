"""SQLite 持久化层：只负责存取，不含业务规则。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

STATUS_PENDING = "pending"        # 待确认（暂留中）
STATUS_CONFIRMED = "confirmed"    # 已确认
STATUS_WAITLISTED = "waitlisted"  # 候补中
STATUS_CANCELLED = "cancelled"    # 已取消（含超时）

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    start INTEGER NOT NULL,
    "end" INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id       INTEGER NOT NULL REFERENCES slots(id),
    status        TEXT NOT NULL,
    hold_deadline INTEGER
);
"""


@dataclass(frozen=True)
class Slot:
    id: int
    start: int  # 含
    end: int    # 不含


@dataclass(frozen=True)
class Request:
    id: int            # 全局受理顺序（自增，确定后不变）
    slot_id: int
    status: str
    hold_deadline: Optional[int]  # 暂留截止时刻；非暂留状态为 None


class Store:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """将一组读写包进一个事务。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ---- 时段 ----
    def add_slot(self, start: int, end: int) -> int:
        cur = self._conn.execute(
            'INSERT INTO slots (start, "end") VALUES (?, ?)', (start, end)
        )
        return int(cur.lastrowid)

    def get_slot(self, slot_id: int) -> Slot:
        row = self._conn.execute(
            'SELECT id, start, "end" FROM slots WHERE id = ?', (slot_id,)
        ).fetchone()
        if row is None:
            raise KeyError(slot_id)
        return Slot(id=row[0], start=row[1], end=row[2])

    def list_slots(self) -> list[Slot]:
        rows = self._conn.execute('SELECT id, start, "end" FROM slots').fetchall()
        return [Slot(id=r[0], start=r[1], end=r[2]) for r in rows]

    # ---- 预约请求 ----
    def create_request(self, slot_id: int, status: str, hold_deadline: Optional[int]) -> int:
        cur = self._conn.execute(
            "INSERT INTO requests (slot_id, status, hold_deadline) VALUES (?, ?, ?)",
            (slot_id, status, hold_deadline),
        )
        return int(cur.lastrowid)

    def get_request(self, request_id: int) -> Request:
        row = self._conn.execute(
            "SELECT id, slot_id, status, hold_deadline FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return Request(id=row[0], slot_id=row[1], status=row[2], hold_deadline=row[3])

    def list_requests(self) -> list[Request]:
        rows = self._conn.execute(
            "SELECT id, slot_id, status, hold_deadline FROM requests ORDER BY id"
        ).fetchall()
        return [Request(id=r[0], slot_id=r[1], status=r[2], hold_deadline=r[3]) for r in rows]

    def set_status(self, request_id: int, status: str, hold_deadline: Optional[int]) -> None:
        self._conn.execute(
            "UPDATE requests SET status = ?, hold_deadline = ? WHERE id = ?",
            (status, hold_deadline, request_id),
        )
