"""Submit independent read jobs without serial foreground waits.

The runner still owns and updates the session on one thread. Only isolated job
execution overlaps; mixed batches retain ordinary per-call supervision.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from genesisai.shared.security import ToolError


foreground_wait = ContextVar('job_foreground_wait', default=None)


def _read_group(runtime, calls):
    if len(calls) < 2:
        return False
    for call in calls:
        descriptor = runtime.catalog.descriptor_for_execution(call['name'])
        spec = descriptor.spec
        if (spec.execution != 'job' or spec.side_effect or spec.permission != 'read'
                or spec.launches_service or spec.awaits_input or spec.requires_acceptance):
            return False
    for identity in runtime.store.record.run_runtime.get('pending_execution_jobs', []):
        info = runtime.jobs.status(identity)
        if info['status'] == 'unknown':
            return False
        if info['status'] in {'queued', 'running', 'stopping'} and (
                info['kind'] != 'builtin_tool' or info['side_effect']):
            return False
    return True


@contextmanager
def submission_batch(runtime, calls):
    try:
        key = tuple((call['id'], call['name']) for call in calls)
        cached = getattr(runtime, '_read_submission_batch', None)
        if cached is None or cached[0] != key:
            cached = (key, _read_group(runtime, calls))
            runtime._read_submission_batch = cached
        parallel = cached[1]
    except (ToolError, KeyError, TypeError, ValueError, AttributeError):
        # Validation and discovery errors belong to the normal tool-result path.
        parallel = False
    token = foreground_wait.set(0 if parallel else None)
    try:
        yield
    finally:
        foreground_wait.reset(token)