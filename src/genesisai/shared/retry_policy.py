"""Provider outage recovery is bounded by elapsed time, independently of tool progress."""

from dataclasses import dataclass, fields
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import random
import time


@dataclass(frozen=True)
class RetryPolicy:
    budget_seconds: float = 600
    initial_delay_seconds: float = 1
    max_delay_seconds: float = 30
    jitter_ratio: float = .2

    @classmethod
    def parse(cls, value=None):
        data = {} if value is None else value
        names = {field.name for field in fields(cls)}
        if not isinstance(data, dict) or data.keys() - names:
            raise ValueError('retry 必须为对象且不能包含未知字段')
        result = cls(**data)
        for name in names:
            number = getattr(result, name)
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ValueError(f'retry.{name} 必须为非负有限数值')
        if result.jitter_ratio > 1 or result.max_delay_seconds < result.initial_delay_seconds:
            raise ValueError('retry 抖动比例须不大于1，最大退避须不小于初始退避')
        return result

    def delay(self, attempt, error):
        headers = getattr(getattr(error, 'response', None), 'headers', {})
        raw = headers.get('retry-after')
        server_delay = 0
        if raw is not None:
            try:
                seconds = float(raw)
                if math.isfinite(seconds):
                    server_delay = max(0, seconds)
            except (ValueError, TypeError):
                try:
                    date = parsedate_to_datetime(str(raw))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    server_delay = max(0, date.timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        # Saturate the exponent before exponentiation, even in a long outage.
        backoff = min(self.max_delay_seconds, self.initial_delay_seconds * 2 ** min(attempt, 30))
        backoff *= random.uniform(1 - self.jitter_ratio, 1 + self.jitter_ratio)
        return max(server_delay, min(self.max_delay_seconds, backoff))
