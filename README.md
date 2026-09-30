# 离线预约库存库

由上层直接调用的 Python 库，预约、候补及其状态持久化在 SQLite。

## 概念

- 一位顾问、一组固定可约时段 `[start, end)`；相邻时段不冲突，不同编号时段可重叠。
- 提交预约：无冲突成为待确认（暂留 15 分钟），否则进入该时段候补。
- 候补按全局受理顺序补位：同一截止时刻的暂留先统一释放，再让当前能容纳的
  最早候补获位，受阻的早到候补不挡住后来者；补位暂留从实际获位时刻起算。
- 所有操作前按历史时刻顺序"追赶"过期与补位，因此长时间未打开、一次跨越多
  个截止点，与逐截止点处理结果完全一致。

## 用法

```python
from booking import BookingService, ManualClock

clock = ManualClock(start=0)
svc = BookingService("booking.db", clock, slots=[(1, 10000, 10100), (2, 10100, 10200)])

req = svc.submit(slot_id=1)   # -> {"status": "pending" | "waitlist", ...}
svc.confirm(req["request_id"])
svc.cancel(req["request_id"])
state = svc.view()            # {"now", "slots", "requests"}，查看也会触发追赶
```

时钟以秒为单位、单调不回退，由调用方注入（`ManualClock` 或任何提供
`now()` 的对象）。测试：`python3 -m pytest tests -q`
