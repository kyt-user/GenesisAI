"""Independent deadlines for a model request, measured with a monotonic clock."""

from dataclasses import asdict, dataclass, fields, replace
import math
import time


class RequestTimeout(TimeoutError):
    def __init__(self, phase, seconds):
        self.phase, self.seconds = phase, seconds
        super().__init__(f'模型请求 {phase} 阶段超过 {seconds:g} 秒')


@dataclass(frozen=True)
class RequestTimeouts:
    connect: float = 30
    first_token: float = 300
    idle: float = 300
    total: float | None = None

    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            value = asdict(value)
        if value is None:
            value = {}
        if not isinstance(value, dict) or value.keys() - {f.name for f in fields(cls)}:
            raise ValueError('request_timeouts 仅接受 connect/first_token/idle/total')
        for key, seconds in value.items():
            if seconds is None and key == 'total':
                continue
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f'request_timeouts.{key} 必须为有限正数，只有 total 可设为 null')
        return cls(**value)

    def limited(self, remaining):
        """An explicit task/retry budget caps the request, including active streams."""
        if not math.isfinite(remaining):
            return self
        return replace(self, total=min(self.total, remaining) if self.total is not None else remaining)

    def as_dict(self):
        return asdict(self)


class RequestClock:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.last_activity = None

    def pending(self):
        if self.last_activity is None:
            phase, seconds, start = 'first_token', self.limits.first_token, self.started
        else:
            phase, seconds, start = 'idle', self.limits.idle, self.last_activity
        deadline = start + seconds
        if self.limits.total is not None and self.started + self.limits.total <= deadline:
            phase, seconds, deadline = 'total', self.limits.total, self.started + self.limits.total
        return phase, seconds, deadline - time.monotonic()

    def check(self):
        phase, seconds, remaining = self.pending()
        if remaining <= 0:
            raise RequestTimeout(phase, seconds)

    def activity(self):
        self.check()
        self.last_activity = time.monotonic()
