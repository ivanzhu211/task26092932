"""预约库存核心服务：提交、确认、取消、查看，以及到期释放与候补补位。

不变式：每次操作结束后，不存在"当前能容纳却仍在候补"的请求；
所有到期处理都按截止时刻的历史顺序重演，因此长时间不打开客户端，
重新查看时补位暂留仍从实际获位的历史时刻起算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .clock import Clock, SystemClock
from .store import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_WAITLISTED,
    Slot,
    Store,
)

HOLD_SECONDS = 15 * 60  # 暂留时长：15 分钟


class BookingError(Exception):
    pass


class SlotNotFound(BookingError):
    pass


class RequestNotFound(BookingError):
    pass


class InvalidState(BookingError):
    """请求当前状态不允许该操作（如确认已取消/已到期的请求）。"""


@dataclass(frozen=True)
class RequestView:
    id: int
    slot_id: int
    status: str
    hold_deadline: Optional[int]


class BookingService:
    def __init__(self, db_path: str, clock: Optional[Clock] = None) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "BookingService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 时段管理（由上层提供固定时段） ----
    def add_slot(self, start: int, end: int) -> int:
        start, end = int(start), int(end)
        if not start < end:
            raise ValueError("slot start must be before end")
        with self._store.transaction():
            return self._store.add_slot(start, end)

    # ---- 四类操作 ----
    def submit(self, slot_id: int) -> int:
        """提交预约：无冲突则待确认，否则进入该时段候补。返回请求 id。"""
        now = self._clock.now()
        with self._store.transaction():
            self._settle(now)
            try:
                slot = self._store.get_slot(slot_id)
            except KeyError:
                raise SlotNotFound(slot_id) from None
            if self._fits(slot):
                return self._store.create_request(
                    slot_id, STATUS_PENDING, now + HOLD_SECONDS
                )
            return self._store.create_request(slot_id, STATUS_WAITLISTED, None)

    def confirm(self, request_id: int) -> None:
        """确认尚未到期的待确认预约；截止时刻及以后不可确认。"""
        now = self._clock.now()
        with self._store.transaction():
            self._settle(now)
            req = self._get_request(request_id)
            if req.status != STATUS_PENDING:
                raise InvalidState(
                    f"request {request_id} is {req.status}, cannot confirm"
                )
            # _settle 已把 deadline <= now 的暂留取消，剩下的必然未到期
            self._store.set_status(request_id, "confirmed", None)

    def cancel(self, request_id: int) -> None:
        """取消候补、待确认或已确认预约；释放时间后立即补位。"""
        now = self._clock.now()
        with self._store.transaction():
            self._settle(now)
            req = self._get_request(request_id)
            if req.status not in (STATUS_WAITLISTED, STATUS_PENDING, "confirmed"):
                raise InvalidState(
                    f"request {request_id} is {req.status}, cannot cancel"
                )
            self._store.set_status(request_id, STATUS_CANCELLED, None)
            if req.status in ACTIVE_STATUSES:
                self._promote(now)

    def view(self) -> list[RequestView]:
        """查看：先按当前时刻完成全部到期释放与补位，再返回快照。"""
        now = self._clock.now()
        with self._store.transaction():
            self._settle(now)
            return [
                RequestView(
                    id=r.id,
                    slot_id=r.slot_id,
                    status=r.status,
                    hold_deadline=r.hold_deadline,
                )
                for r in self._store.list_requests()
            ]

    # ---- 内部 ----
    def _get_request(self, request_id: int):
        try:
            return self._store.get_request(request_id)
        except KeyError:
            raise RequestNotFound(request_id) from None

    def _fits(self, slot: Slot) -> bool:
        """该时段当前是否可容纳：与所有占用（待确认/已确认）的时段均不重叠。"""
        slots = {s.id: s for s in self._store.list_slots()}
        for req in self._store.list_requests():
            if req.status not in ACTIVE_STATUSES:
                continue
            other = slots[req.slot_id]
            if slot.start < other.end and other.start < slot.end:
                return False
        return True

    def _settle(self, now: int) -> None:
        """按历史顺序处理所有截止时刻 <= now 的暂留超时与补位。

        每一轮：取最早的到期时刻 t，释放该时刻的全部到期暂留，
        再在 t 时刻统一补位（补位暂留从 t 起算 15 分钟）。
        补位可能产生新的、同样 <= now 的截止点，循环直到处理完。
        """
        while True:
            requests = self._store.list_requests()
            expired = [
                r
                for r in requests
                if r.status == STATUS_PENDING
                and r.hold_deadline is not None
                and r.hold_deadline <= now
            ]
            if not expired:
                return
            t = min(r.hold_deadline for r in expired)
            for r in expired:
                if r.hold_deadline == t:
                    self._store.set_status(r.id, STATUS_CANCELLED, None)
            self._promote(t)

    def _promote(self, at: int) -> None:
        """在 at 时刻补位：反复让当前能容纳的最早候补获位，直到没有可容纳者。

        受阻的早到候补不挡住可容纳的后来者；获位即从 at 起算暂留，
        立即产生时间占用。
        """
        while True:
            slots = {s.id: s for s in self._store.list_slots()}
            promoted = False
            for req in self._store.list_requests():
                if req.status != STATUS_WAITLISTED:
                    continue
                if self._fits(slots[req.slot_id]):
                    self._store.set_status(
                        req.id, STATUS_PENDING, at + HOLD_SECONDS
                    )
                    promoted = True
                    break
            if not promoted:
                return
