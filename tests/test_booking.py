import os
import tempfile
import unittest

from booking import (
    BookingService,
    HOLD_SECONDS,
    InvalidState,
    ManualClock,
    RequestNotFound,
    SlotNotFound,
)

T0 = 1_000_000  # 基准时刻；所有时段都在它之后开始


def make_service(db_path=":memory:", start=T0):
    clock = ManualClock(start)
    return BookingService(db_path, clock=clock), clock


def snapshot(svc):
    return [(v.id, v.slot_id, v.status, v.hold_deadline) for v in svc.view()]


def status_of(svc, request_id):
    for v in svc.view():
        if v.id == request_id:
            return v.status
    raise AssertionError(f"request {request_id} not found")


class BasicFlowTest(unittest.TestCase):
    def test_submit_without_conflict_becomes_pending(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        r1 = svc.submit(s1)
        self.assertEqual(
            snapshot(svc), [(r1, s1, "pending", T0 + HOLD_SECONDS)]
        )
        svc.close()

    def test_submit_with_conflict_goes_waitlisted(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)  # 与 s1 重叠
        svc.submit(s1)
        r2 = svc.submit(s2)
        self.assertEqual(status_of(svc, r2), "waitlisted")
        svc.close()

    def test_adjacent_slots_do_not_conflict(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 2000, T0 + 3000)  # 相邻不算冲突
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)
        self.assertEqual(status_of(svc, r1), "pending")
        self.assertEqual(status_of(svc, r2), "pending")
        svc.close()

    def test_confirm_before_deadline(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        r1 = svc.submit(s1)
        clock.advance(HOLD_SECONDS - 1)
        svc.confirm(r1)
        self.assertEqual(snapshot(svc), [(r1, s1, "confirmed", None)])
        svc.close()

    def test_confirm_at_deadline_fails_and_request_is_cancelled(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        r1 = svc.submit(s1)
        clock.advance(HOLD_SECONDS)  # 恰好到达截止时刻
        with self.assertRaises(InvalidState):
            svc.confirm(r1)
        self.assertEqual(status_of(svc, r1), "cancelled")
        svc.close()

    def test_confirmed_not_affected_by_hold_deadline(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        r1 = svc.submit(s1)
        svc.confirm(r1)
        clock.advance(HOLD_SECONDS * 10)
        self.assertEqual(status_of(svc, r1), "confirmed")
        svc.close()

    def test_cancel_each_cancellable_status(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)
        r_pending = svc.submit(s1)
        r_wait = svc.submit(s2)
        svc.cancel(r_wait)
        self.assertEqual(status_of(svc, r_wait), "cancelled")
        svc.cancel(r_pending)
        self.assertEqual(status_of(svc, r_pending), "cancelled")
        r_conf = svc.submit(s1)
        svc.confirm(r_conf)
        svc.cancel(r_conf)
        self.assertEqual(status_of(svc, r_conf), "cancelled")
        svc.close()

    def test_cancelled_request_cannot_be_confirmed_or_cancelled_again(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        r1 = svc.submit(s1)
        svc.cancel(r1)
        with self.assertRaises(InvalidState):
            svc.confirm(r1)
        with self.assertRaises(InvalidState):
            svc.cancel(r1)
        svc.close()

    def test_unknown_ids_raise(self):
        svc, _ = make_service()
        with self.assertRaises(SlotNotFound):
            svc.submit(999)
        with self.assertRaises(RequestNotFound):
            svc.confirm(999)
        with self.assertRaises(RequestNotFound):
            svc.cancel(999)
        svc.close()

    def test_clock_cannot_go_backwards(self):
        clock = ManualClock(100)
        with self.assertRaises(ValueError):
            clock.set(99)
        with self.assertRaises(ValueError):
            clock.advance(-1)


class WaitlistPromotionTest(unittest.TestCase):
    def test_expiry_promotes_waitlist_with_historical_deadline(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)
        clock.advance(HOLD_SECONDS + 5)  # r1 在 T0+900 到期
        view = {v.id: v for v in svc.view()}
        self.assertEqual(view[r1].status, "cancelled")
        self.assertEqual(view[r2].status, "pending")
        # 补位暂留从实际获位的历史时刻 T0+900 起算，而不是查看时刻
        self.assertEqual(view[r2].hold_deadline, T0 + HOLD_SECONDS * 2)
        svc.close()

    def test_cancel_promotes_immediately(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)
        clock.advance(60)
        svc.cancel(r1)
        view = {v.id: v for v in svc.view()}
        self.assertEqual(view[r2].status, "pending")
        self.assertEqual(view[r2].hold_deadline, T0 + 60 + HOLD_SECONDS)
        svc.close()

    def test_blocked_earlier_waitlist_does_not_block_later_one(self):
        svc, _ = make_service()
        s_big = svc.add_slot(T0 + 1000, T0 + 3000)
        s_small = svc.add_slot(T0 + 4000, T0 + 5000)  # 与 s_big 不重叠
        s_mid = svc.add_slot(T0 + 2000, T0 + 4500)    # 与两者都重叠
        r_big = svc.submit(s_big)      # 占用 s_big
        r_mid = svc.submit(s_mid)      # 冲突 → 候补（需要 s_big 释放）
        r_small = svc.submit(s_small)  # 冲突于 s_mid？不，s_mid 未占用 → 直接待确认
        self.assertEqual(status_of(svc, r_small), "pending")
        # 取消 s_small 的占用后，候补 r_mid 仍因 s_big 受阻
        svc.cancel(r_small)
        self.assertEqual(status_of(svc, r_mid), "waitlisted")
        svc.cancel(r_big)
        self.assertEqual(status_of(svc, r_mid), "pending")
        svc.close()

    def test_overlapping_waitlists_cannot_both_be_promoted(self):
        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)  # 与 s1 重叠
        s3 = svc.add_slot(T0 + 1800, T0 + 2800)  # 与 s2 重叠、与 s1 不重叠
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)  # 候补
        r3 = svc.submit(s3)  # 候补（s2 未占用，但 s1 占用？s3 与 s1 不重叠 → 待确认）
        # r3 与占用中的 r1 不重叠，应为 pending；重新设计：让 r3 也候补
        svc.close()

        svc, clock = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)
        s3 = svc.add_slot(T0 + 1200, T0 + 2200)  # 与 s1、s2 都重叠
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)  # 候补
        r3 = svc.submit(s3)  # 候补
        svc.cancel(r1)
        # 最早的 r2 获位；r3 与 r2 重叠，不能同时获位
        self.assertEqual(status_of(svc, r2), "pending")
        self.assertEqual(status_of(svc, r3), "waitlisted")
        svc.close()

    def test_existing_waitlist_has_priority_over_new_submit(self):
        svc, _ = make_service()
        s1 = svc.add_slot(T0 + 1000, T0 + 2000)
        s2 = svc.add_slot(T0 + 1500, T0 + 2500)
        r1 = svc.submit(s1)
        r2 = svc.submit(s2)  # 候补
        svc.cancel(r1)       # 立即补位给 r2
        self.assertEqual(status_of(svc, r2), "pending")
        r3 = svc.submit(s1)  # 与 r2 冲突 → 候补
        self.assertEqual(status_of(svc, r3), "waitlisted")
        svc.close()


class SimultaneousExpiryTest(unittest.TestCase):
    """两个相邻暂留同时到期：最早候补需要两者的长时段，必须先释放完再统一补位。"""

    def test_earliest_waitlist_wins_over_later_short_one(self):
        svc, clock = make_service()
        s_a = svc.add_slot(T0 + 1000, T0 + 2000)
        s_b = svc.add_slot(T0 + 2000, T0 + 3000)  # 与 s_a 相邻
        s_long = svc.add_slot(T0 + 1000, T0 + 3000)  # 覆盖 a+b
        # 两个相邻暂留，同一时刻提交，同一截止时刻
        r_a = svc.submit(s_a)
        r_b = svc.submit(s_b)
        # 候补：早到的需要整段，晚到的只需要 s_a
        w_long = svc.submit(s_long)
        w_short = svc.submit(s_a)
        self.assertEqual(status_of(svc, w_long), "waitlisted")
        self.assertEqual(status_of(svc, w_short), "waitlisted")
        # 到达共同截止时刻：两个暂留同时释放
        clock.advance(HOLD_SECONDS)
        view = {v.id: v for v in svc.view()}
        self.assertEqual(view[r_a].status, "cancelled")
        self.assertEqual(view[r_b].status, "cancelled")
        # 最早候补获位；若逐个释放并逐个补位，w_short 会抢先占用 s_a
        self.assertEqual(view[w_long].status, "pending")
        self.assertEqual(view[w_long].hold_deadline, T0 + HOLD_SECONDS * 2)
        self.assertEqual(view[w_short].status, "waitlisted")
        svc.close()


class LazyCatchUpTest(unittest.TestCase):
    """一次跨过多个 15 分钟截止点，结果必须与逐个截止点及时处理完全一致。"""

    def _build_scenario(self, db_path=":memory:"):
        svc, clock = make_service(db_path)
        # 链 1：slot1 上 p1 → w1 → w2 依次获位再依次超时
        s1 = svc.add_slot(T0 + 10000, T0 + 20000)
        p1 = svc.submit(s1)            # 截止 T0+900
        w1 = svc.submit(s1)            # 候补，T0+900 获位，截止 T0+1800
        w2 = svc.submit(s1)            # 候补，T0+1800 获位，截止 T0+2700
        # 链 2：不同起始时刻，截止点与链 1 交错
        s2 = svc.add_slot(T0 + 30000, T0 + 40000)
        clock.advance(100)
        q1 = svc.submit(s2)            # 截止 T0+1000
        v1 = svc.submit(s2)            # 候补，T0+1000 获位，截止 T0+1900
        # 链 3：确认后不再超时，长期占用
        s3 = svc.add_slot(T0 + 50000, T0 + 60000)
        c1 = svc.submit(s3)
        svc.confirm(c1)
        return svc, clock, (p1, w1, w2, q1, v1, c1)

    def test_jump_equals_step_by_step(self):
        deadlines = [T0 + 900, T0 + 1000, T0 + 1800, T0 + 1900, T0 + 2700]

        # 参考组：每个截止点都及时查看（触发当刻释放与补位）
        ref_svc, ref_clock, _ = self._build_scenario()
        for d in deadlines:
            ref_clock.set(d)
            ref_svc.view()
        ref_clock.set(T0 + 5000)
        ref_final = snapshot(ref_svc)
        ref_svc.close()

        # 跳跃组：客户端长时间未打开，一次跨过全部截止点
        jump_svc, jump_clock, _ = self._build_scenario()
        jump_clock.set(T0 + 5000)
        jump_final = snapshot(jump_svc)
        jump_svc.close()

        self.assertEqual(jump_final, ref_final)

    def test_jump_intermediate_observation_matches(self):
        # 跨到中间时刻观察，也要等于逐步处理到该时刻
        svc_step, clock_step, _ = self._build_scenario()
        for d in (T0 + 900, T0 + 1000, T0 + 1800):
            clock_step.set(d)
            svc_step.view()
        clock_step.set(T0 + 1850)
        expected = snapshot(svc_step)
        svc_step.close()

        svc_jump, clock_jump, _ = self._build_scenario()
        clock_jump.set(T0 + 1850)
        actual = snapshot(svc_jump)
        svc_jump.close()
        self.assertEqual(actual, expected)

    def test_expired_holds_not_counted_after_reopen(self):
        # 客户端长时间未打开：关闭后用同一数据库重开，过期暂留不得再占用
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "booking.db")
            svc, clock = make_service(db)
            s1 = svc.add_slot(T0 + 1000, T0 + 2000)
            s2 = svc.add_slot(T0 + 1500, T0 + 2500)
            r1 = svc.submit(s1)
            r2 = svc.submit(s2)  # 候补
            svc.close()

            clock2 = ManualClock(T0 + 1000)  # r1 已到期，r2 补位暂留仍有效
            svc2 = BookingService(db, clock=clock2)
            view = {v.id: v for v in svc2.view()}
            self.assertEqual(view[r1].status, "cancelled")
            self.assertEqual(view[r2].status, "pending")
            # 补位从实际获位的历史时刻起算，且该暂留本身也已超时
            self.assertEqual(view[r2].hold_deadline, T0 + HOLD_SECONDS * 2)
            svc2.close()

            # 再次重开，跨过 r2 的补位截止点：该暂留也已到期取消
            clock3 = ManualClock(T0 + HOLD_SECONDS * 3)
            svc3 = BookingService(db, clock=clock3)
            view3 = {v.id: v for v in svc3.view()}
            self.assertEqual(view3[r2].status, "cancelled")
            svc3.close()


if __name__ == "__main__":
    unittest.main()
