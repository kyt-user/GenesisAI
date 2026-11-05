"""Defaults for the task deadline and individual model requests."""
from contextvars import ContextVar

# Absolute monotonic deadline of the tool currently using a host transport.
tool_deadline = ContextVar("tool_deadline", default=None)

DEFAULT_TASK_SECONDS = 300
DEFAULT_REQUEST_TIMEOUT = 60
