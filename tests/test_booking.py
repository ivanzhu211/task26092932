import pytest

from booking import BookingService, ManualClock
from booking.service import HOLD_SECONDS


def make_service(tmp_path, clock, slots, name="test.db"):
    return BookingService(str(tmp_path / name), clock, slots)


def by_id(view):
    return {r["request_id"]: r for r in view["requests"]}


# ---- 基本行为 ----

def test_submit_pending_then_confirm(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 200)])
    r = svc.submit(1)
    assert r["status"] == "pending"
    assert r["hold_deadline"] == HOLD_SECONDS
    clock.set(100)
    r = svc.confirm(r["request_id"])
    assert r["status"] == "confirmed"
    assert r["hold_deadline"] is None
    # 确认后不再受暂留截止影响
    clock.set(100000)
    assert by_id(svc.view())[r["request_id"]]["status"] == "confirmed"


def test_pending_expires_at_deadline(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 200)])
    rid = svc.submit(1)["request_id"]
    clock.set(HOLD_SECONDS - 1)
    assert by_id(svc.view())[rid]["status"] == "pending"
    # 到达截止时刻即视为过期，且不能再确认
    clock.set(HOLD_SECONDS)
    assert by_id(svc.view())[rid]["status"] == "cancelled"
    with pytest.raises(ValueError):
        svc.confirm(rid)


def test_cancelled_cannot_be_restored(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 200)])
    rid = svc.submit(1)["request_id"]
    svc.cancel(rid)
    with pytest.raises(ValueError):
        svc.confirm(rid)
    with pytest.raises(ValueError):
        svc.cancel(rid)


def test_adjacent_slots_do_not_conflict(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 110), (2, 110, 120)])
    assert svc.submit(1)["status"] == "pending"
    assert svc.submit(2)["status"] == "pending"


def test_blocked_waitlist_does_not_block_later_fit(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock,
                       [(1, 100, 120), (2, 110, 130), (3, 200, 210)])
    svc.submit(1)                      # 占用 [100,120)
    w1 = svc.submit(2)                 # 与 1 重叠 -> 候补
    assert w1["status"] == "waitlist"
    r3 = svc.submit(3)                 # 不重叠 -> 直接暂留
    assert r3["status"] == "pending"


def test_waitlist_promoted_on_cancel_and_hold_starts_then(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 200)])
    r1 = svc.submit(1)
    w = svc.submit(1)
    assert w["status"] == "waitlist"
    clock.set(50)
    svc.cancel(r1["request_id"])
    got = by_id(svc.view())[w["request_id"]]
    assert got["status"] == "pending"
    assert got["promoted_at"] == 50
    assert got["hold_deadline"] == 50 + HOLD_SECONDS


def test_waitlist_priority_over_new_submit_at_same_moment(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [(1, 100, 200)])
    r1 = svc.submit(1)                 # 截止于 900
    w = svc.submit(1)                  # 候补
    clock.set(HOLD_SECONDS)            # 恰好在截止时刻
    r3 = svc.submit(1)                 # 先释放、补位，再处理本次提交
    reqs = by_id(svc.view())
    assert reqs[r1["request_id"]]["status"] == "cancelled"
    assert reqs[w["request_id"]]["status"] == "pending"
    assert reqs[w["request_id"]]["hold_deadline"] == 2 * HOLD_SECONDS
    assert r3["status"] == "waitlist"


# ---- 场景一：两个相邻暂留同时到期，最早候补获位 ----

def test_simultaneous_adjacent_expiry_earliest_waitlist_wins(tmp_path):
    clock = ManualClock(0)
    svc = make_service(tmp_path, clock, [
        (1, 0, 10),      # 第一段
        (2, 10, 20),     # 相邻第二段（不冲突）
        (3, 0, 20),      # 覆盖两段的长时段
    ])
    r1 = svc.submit(1)                 # 暂留 [0,10)，截止 900
    r2 = svc.submit(2)                 # 暂留 [10,20)，截止 900
    w_long = svc.submit(3)             # 需要两段 -> 候补
    w_short = svc.submit(2)            # 只需第二段 -> 候补
    assert w_long["status"] == "waitlist"
    assert w_short["status"] == "waitlist"

    clock.set(HOLD_SECONDS)            # 两个暂留同时到期
    reqs = by_id(svc.view())
    assert reqs[r1["request_id"]]["status"] == "cancelled"
    assert reqs[r2["request_id"]]["status"] == "cancelled"
    # 同一时刻统一释放后再补位：最早候补获位，较晚候补继续等待
    assert reqs[w_long["request_id"]]["status"] == "pending"
    assert reqs[w_long["request_id"]]["promoted_at"] == HOLD_SECONDS
    assert reqs[w_long["request_id"]]["hold_deadline"] == 2 * HOLD_SECONDS
    assert reqs[w_short["request_id"]]["status"] == "waitlist"


# ---- 场景二：一次跨过多个截止点 == 逐截止点处理 ----

MULTI_SLOTS = [
    (1, 10000, 10010),
    (2, 10005, 10015),   # 与 1 重叠
    (3, 10020, 10030),   # 独立
]


def run_multi_scenario(tmp_path, clock, stepwise, name):
    svc = make_service(tmp_path, clock, MULTI_SLOTS, name)
    r1 = svc.submit(1)                 # 暂留，截止 900
    w2 = svc.submit(2)                 # 候补
    w3 = svc.submit(1)                 # 候补
    r4 = svc.submit(3)                 # 暂留
    w5 = svc.submit(3)                 # 候补
    clock.set(1)
    svc.confirm(r4["request_id"])      # 确认，长期占用 3
    # 目标时刻 2000：跨过 900 与 1800 两个截止点
    if stepwise:
        for t in range(2, 2001):
            clock.set(t)
            svc.view()
    else:
        clock.set(2000)
    view = svc.view()
    svc.close()
    return view


def test_skip_multiple_deadlines_matches_stepwise(tmp_path):
    jumped = run_multi_scenario(tmp_path, ManualClock(0), False, "jump.db")
    stepped = run_multi_scenario(tmp_path, ManualClock(0), True, "step.db")
    # 完整状态（含各请求状态与截止时间）逐项一致
    assert jumped["requests"] == stepped["requests"]

    reqs = by_id(jumped)
    # 900: r1 过期 -> w2 获位（截止 1800）；1800: w2 过期 -> w3 获位（截止 2700）
    assert reqs[1]["status"] == "cancelled"
    assert reqs[2]["status"] == "cancelled"
    assert reqs[2]["promoted_at"] == HOLD_SECONDS
    assert reqs[3]["status"] == "pending"
    assert reqs[3]["promoted_at"] == 2 * HOLD_SECONDS
    assert reqs[3]["hold_deadline"] == 3 * HOLD_SECONDS
    assert reqs[4]["status"] == "confirmed"
    assert reqs[5]["status"] == "waitlist"   # 3 号时段被确认占用，永远等位


# ---- 持久化与长时间未打开 ----

def test_state_survives_reopen_and_lazy_expiry(tmp_path):
    path = str(tmp_path / "persist.db")
    clock = ManualClock(0)
    svc = BookingService(path, clock, [(1, 100, 200)])
    rid = svc.submit(1)["request_id"]
    svc.close()

    # 客户端长时间未打开，直接跳到远未来
    clock.set(10 ** 9)
    svc2 = BookingService(path, clock, [(1, 100, 200)])
    reqs = by_id(svc2.view())
    assert reqs[rid]["status"] == "cancelled"
    svc2.close()
