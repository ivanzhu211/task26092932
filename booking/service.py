"""预约库存服务：提交、确认、取消、查看。

时间语义：
- 所有操作共用注入的可控时钟，精度到秒。
- 任何操作前先按历史时刻顺序"追赶"：逐批释放同一截止时刻的暂留，
  立即在该历史时刻补位，补位暂留从实际获位时刻起算 15 分钟。
- 因此一次跨过多个截止点与逐点处理结果完全一致。
"""

from .clock import ManualClock
from .store import Store

HOLD_SECONDS = 15 * 60

ACTIVE_STATUSES = ("pending", "confirmed")


class BookingService:
    def __init__(self, db_path: str, clock: ManualClock,
                 slots: list[tuple[int, int, int]]) -> None:
        self._store = Store(db_path)
        self._clock = clock
        conn = self._store.conn()
        for slot_id, start, end in slots:
            if end <= start:
                raise ValueError(f"slot {slot_id}: end must be after start")
            conn.execute(
                "INSERT OR IGNORE INTO slots(slot_id, start, end) VALUES (?, ?, ?)",
                (slot_id, start, end),
            )
        conn.commit()

    # ---- 内部：时间追赶与补位 ----

    def _overlaps(self, conn, slot_id: int) -> bool:
        """该时段是否与任何占用（待确认/已确认）的时段重叠。"""
        row = conn.execute(
            """
            SELECT 1 FROM requests r
            JOIN slots a ON a.slot_id = r.slot_id
            JOIN slots b ON b.slot_id = ?
            WHERE r.status IN ('pending','confirmed')
              AND a.start < b."end" AND b.start < a."end"
            LIMIT 1
            """,
            (slot_id,),
        ).fetchone()
        return row is not None

    def _promote(self, conn, at: int) -> None:
        """在时刻 at 反复让当前能容纳的最早候补获位，直到没有可容纳者。"""
        while True:
            candidate = None
            for r in conn.execute(
                "SELECT request_id, slot_id FROM requests "
                "WHERE status = 'waitlist' ORDER BY request_id"
            ):
                if not self._overlaps(conn, r["slot_id"]):
                    candidate = r
                    break
            if candidate is None:
                return
            conn.execute(
                "UPDATE requests SET status='pending', promoted_at=?, "
                "hold_deadline=? WHERE request_id=?",
                (at, at + HOLD_SECONDS, candidate["request_id"]),
            )

    def _catch_up(self, conn, now: int) -> None:
        """按截止时刻先后处理所有不晚于 now 的暂留超时并即时补位。"""
        while True:
            row = conn.execute(
                "SELECT MIN(hold_deadline) AS d FROM requests "
                "WHERE status='pending' AND hold_deadline <= ?",
                (now,),
            ).fetchone()
            deadline = row["d"]
            if deadline is None:
                return
            conn.execute(
                "UPDATE requests SET status='cancelled', hold_deadline=NULL "
                "WHERE status='pending' AND hold_deadline = ?",
                (deadline,),
            )
            self._promote(conn, deadline)

    def _txn(self, fn):
        conn = self._store.conn()
        now = self._clock.now()
        try:
            conn.execute("BEGIN")
            self._catch_up(conn, now)
            result = fn(conn, now)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise

    # ---- 对外操作 ----

    def submit(self, slot_id: int) -> dict:
        def op(conn, now):
            slot = conn.execute(
                "SELECT slot_id FROM slots WHERE slot_id=?", (slot_id,)
            ).fetchone()
            if slot is None:
                raise KeyError(f"unknown slot {slot_id}")
            if self._overlaps(conn, slot_id):
                status, deadline = "waitlist", None
            else:
                status, deadline = "pending", now + HOLD_SECONDS
            cur = conn.execute(
                "INSERT INTO requests(slot_id, status, created_at, promoted_at, hold_deadline) "
                "VALUES (?, ?, ?, ?, ?)",
                (slot_id, status, now, now if status == "pending" else None, deadline),
            )
            return self._get(conn, cur.lastrowid)
        return self._txn(op)

    def confirm(self, request_id: int) -> dict:
        def op(conn, now):
            req = self._get(conn, request_id)
            if req["status"] != "pending":
                raise ValueError(f"request {request_id} is {req['status']}, cannot confirm")
            conn.execute(
                "UPDATE requests SET status='confirmed', hold_deadline=NULL "
                "WHERE request_id=?",
                (request_id,),
            )
            return self._get(conn, request_id)
        return self._txn(op)

    def cancel(self, request_id: int) -> dict:
        def op(conn, now):
            req = self._get(conn, request_id)
            if req["status"] == "cancelled":
                raise ValueError(f"request {request_id} already cancelled")
            was_active = req["status"] in ACTIVE_STATUSES
            conn.execute(
                "UPDATE requests SET status='cancelled', hold_deadline=NULL "
                "WHERE request_id=?",
                (request_id,),
            )
            if was_active:
                self._promote(conn, now)
            return self._get(conn, request_id)
        return self._txn(op)

    def view(self) -> dict:
        def op(conn, now):
            slots = [dict(r) for r in conn.execute(
                "SELECT slot_id, start, \"end\" FROM slots ORDER BY slot_id")]
            requests = [self._get(conn, r["request_id"]) for r in conn.execute(
                "SELECT request_id FROM requests ORDER BY request_id")]
            return {"now": now, "slots": slots, "requests": requests}
        return self._txn(op)

    def _get(self, conn, request_id: int) -> dict:
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown request {request_id}")
        return dict(row)

    def close(self) -> None:
        self._store.close()
