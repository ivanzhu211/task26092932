"""可控时钟：库内所有时间均为整数秒（Unix 时间戳），且单调不回退。"""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """时钟协议：返回当前时刻（整数秒）。"""

    def now(self) -> int: ...


class SystemClock:
    """真实时钟，精度到秒。"""

    def now(self) -> int:
        return int(time.time())


class ManualClock:
    """测试用可控时钟，只允许前进，不允许回退。"""

    def __init__(self, start: int = 0) -> None:
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def set(self, value: int) -> None:
        value = int(value)
        if value < self._now:
            raise ValueError("clock cannot go backwards")
        self._now = value

    def advance(self, seconds: int) -> None:
        seconds = int(seconds)
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._now += seconds
