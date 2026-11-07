"""Layered MCP waits; only increasing, correlated progress renews idle time."""
from dataclasses import asdict, dataclass, fields
import math
import time
from uuid import uuid4

from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline


@dataclass(frozen=True)
class McpTimeouts:
    connect: float = 30
    first_response: float = 300
    idle: float = 300
    total: float | None = None

    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            return value
        value = {} if value is None else value
        if not isinstance(value, dict) or value.keys() - {field.name for field in fields(cls)}:
            raise ValueError('MCP request_timeouts 仅接受 connect/first_response/idle/total')
        for name, seconds in value.items():
            if name == 'total' and seconds is None:
                continue
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError('MCP 等待时限必须为有限正数；total 可为 null')
        return cls(**value)

    def as_dict(self):
        return asdict(self)


class RequestClock:
    def __init__(self, limits):
        self.limits = McpTimeouts.parse(limits)
        self.started = time.monotonic()
        self.last_activity = None
        self.progress = None
        self.token = uuid4().hex
        self.outer_deadline = tool_deadline.get()

    def remaining(self):
        now = time.monotonic()
        phase = 'first_response' if self.last_activity is None else 'idle'
        seconds = getattr(self.limits, phase)
        deadline = (self.started if self.last_activity is None else self.last_activity) + seconds
        if self.limits.total is not None:
            deadline = min(deadline, self.started + self.limits.total)
        if self.outer_deadline is not None:
            deadline = min(deadline, self.outer_deadline)
        left = deadline - now
        if left <= 0:
            raise ToolError('tool_timeout', 'MCP 请求等待超时；未自动重发，请核对远端结果')
        return left

    def params(self, values):
        return {**values, '_meta': {**values.get('_meta', {}), 'progressToken': self.token}}

    def observe(self, message):
        self.remaining()
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or 'id' in message:
            return
        if message.get('method') != 'notifications/progress':
            return
        params = message.get('params')
        if not isinstance(params, dict) or params.get('progressToken') != self.token:
            return
        progress = params.get('progress')
        if type(progress) not in (int, float) or not math.isfinite(progress):
            return
        if self.progress is None or progress > self.progress:
            self.progress = progress
            self.last_activity = time.monotonic()
