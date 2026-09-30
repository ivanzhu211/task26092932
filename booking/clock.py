"""可控时钟：时间精确到秒，单调不回退。"""


class ManualClock:
    def __init__(self, start: int = 0) -> None:
        self._now = int(start)

    def now(self) -> int:
        return self._now

    def set(self, t: int) -> None:
        t = int(t)
        if t < self._now:
            raise ValueError(f"clock cannot go backwards: {t} < {self._now}")
        self._now = t

    def advance(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("cannot advance by a negative amount")
        self._now += int(seconds)
