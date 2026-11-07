"""One cancellable elapsed-time and resource budget for a public network operation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
import math
import time

from genesisai.shared.retry_policy import RetryPolicy
from genesisai.shared.run_control import current_run, cancellation_requested
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline

active_deadline = ContextVar('public_network_deadline', default=None)
active_limits = ContextVar('public_network_limits', default=None)
active_scope = ContextVar('public_network_scope', default=None)


@dataclass(frozen=True)
class NetworkLimits:
    total_seconds: float = 120
    io_seconds: float = 30
    response_bytes: int = 32 * 1024**2
    decoded_bytes: int = 64 * 1024**2
    max_redirects: int = 10
    retry_initial_seconds: float = .5
    retry_max_seconds: float = 10

    @classmethod
    def parse(cls, supplied=None):
        if isinstance(supplied, cls):
            return supplied
        values = {} if supplied is None else supplied
        if not isinstance(values, dict) or values.keys() - {field.name for field in fields(cls)}:
            raise ValueError('网络预算包含未知设置')
        result = cls(**values)
        for field in fields(cls):
            value = getattr(result, field.name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (value == 0 and field.name != 'max_redirects'):
                raise ValueError('网络预算必须为正有限数值：' + field.name)
        if any(type(getattr(result, name)) is not int for name in ('response_bytes', 'decoded_bytes', 'max_redirects')):
            raise ValueError('网络字节和跳转预算必须为整数')
        if result.retry_max_seconds < result.retry_initial_seconds:
            raise ValueError('最大网络退避不能小于初始退避')
        return result

    def retry(self):
        return RetryPolicy(initial_delay_seconds=self.retry_initial_seconds,
                           max_delay_seconds=self.retry_max_seconds, jitter_ratio=.2)


def remaining():
    if cancellation_requested():
        raise ToolError('user_cancelled', '网络操作已取消')
    end = active_deadline.get()
    if end is None:
        raise RuntimeError('网络请求缺少统一预算')
    value = end - time.monotonic()
    if value <= 0:
        raise ToolError('timeout', '网络总时间预算耗尽', True)
    return value


@contextmanager
def network_budget(limits):
    ends = [time.monotonic() + limits.total_seconds]
    ends.extend(value for value in (active_deadline.get(), tool_deadline.get()) if value is not None)
    token = active_deadline.set(min(ends))
    try:
        remaining()
        yield
    finally:
        active_deadline.reset(token)


def pause(seconds):
    delay = min(seconds, remaining())
    end = time.monotonic() + delay
    while time.monotonic() < end:
        remaining()
        control = current_run.get()
        duration = min(.1, end - time.monotonic())
        if duration > 0:
            control.cancelled.wait(duration) if control else time.sleep(duration)
    remaining()
