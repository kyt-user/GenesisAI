"""Defaults for the task deadline and individual model requests."""
from contextvars import ContextVar

# Absolute monotonic deadline of the tool currently using a host transport.
tool_deadline = ContextVar("tool_deadline", default=None)
execution_limits = ContextVar('execution_resource_limits', default={})

DEFAULT_TASK_SECONDS = None
DEFAULT_MAX_ROUNDS = None
DEFAULT_MAX_TOOLS = None
PROGRESS_CHECKPOINT_ROUNDS = 20


def exhausted(limit, used):
    """Only an explicitly configured finite budget can be exhausted."""
    return limit is not None and used >= limit


def remaining(limit, used):
    return None if limit is None else max(0, limit - used)
