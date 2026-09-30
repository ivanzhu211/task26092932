# 离线预约库存库

纯 Python + SQLite 的本地预约库存库，由上层直接调用，无任何外部服务依赖。

## 功能

- 上层登记固定的可约时段（区间含开始、不含结束；不同编号时段可重叠）。
- 客户提交预约：无冲突则成为待确认（暂留 15 分钟），否则进入该时段的候补。
- 待确认在截止时刻前可确认；截止时刻及以后自动转为已取消，不可恢复。
- 取消候补 / 待确认 / 已确认预约，释放时间后立即补位。
- 候补按全局受理顺序补位：反复让当前能容纳的最早候补获位，受阻者不挡住后来者。
- 所有状态持久化在 SQLite；到期处理按历史截止时刻重演，因此客户端长时间未打开，
  重新查看时补位暂留仍从实际获位的历史时刻起算，结果与逐个截止点及时处理完全一致。

## 用法

```python
from booking import BookingService, ManualClock

clock = ManualClock(start=1_000_000)   # 生产环境可省略，默认 SystemClock
svc = BookingService("booking.db", clock=clock)

slot_id = svc.add_slot(start=1_005_000, end=1_006_000)  # 上层登记固定时段
req_id = svc.submit(slot_id)     # 无冲突 → 待确认；冲突 → 候补
svc.confirm(req_id)              # 截止时刻前确认
svc.cancel(req_id)               # 取消候补/待确认/已确认
for v in svc.view():             # 查看（先完成到期释放与补位）
    print(v.id, v.slot_id, v.status, v.hold_deadline)
svc.close()
```

- 时间一律为整数秒（Unix 时间戳），时钟单调不回退；`ManualClock` 用于测试。
- 状态：`pending`（待确认/暂留中）、`confirmed`、`waitlisted`、`cancelled`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
