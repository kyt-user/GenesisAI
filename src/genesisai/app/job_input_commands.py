"""Explicit user input uses committed job state, without mutating a live Session."""
import json
import time
from types import SimpleNamespace

from genesisai.shared.ids import uid
from genesisai.shared.time_limits import tool_deadline


def is_job_input(text):
    words = text.split(None, 1)
    return bool(words and words[0].casefold() == '/job-input')


def cli_input_text(text):
    return '/job-input [内容未记录]' if is_job_input(text) else text


def execute_job_input(store, logger, command):
    from genesisai.core.jobs.service import JobService
    from genesisai.core.extensions.tools.kernel.write_job_input.spec import SPEC
    if store is None:
        raise ValueError('请先选择拥有该作业的会话')
    parts = command.split(None, 1)
    if not is_job_input(command) or len(parts) != 2:
        raise ValueError('用法：/job-input {"job_id":"job_...","action":"write","text":"内容","append_newline":true}；密钥可改用 secret_from_env')
    try:
        args = json.loads(parts[1])
    except (ValueError, TypeError) as exc:
        raise ValueError('作业输入需要有效 JSON 对象') from exc
    if not isinstance(args, dict):
        raise ValueError('作业输入需要 JSON 对象')
    # Only stable namespace identifiers are shared with the runner. Validation
    # reads the committed Session and task/grant state via Session.inspect.
    detached = SimpleNamespace(id=store.id, root=store.root)
    jobs = JobService(detached, logger.journal.clean, logger)
    deadline = min(tool_deadline.get() or float('inf'), time.monotonic() + SPEC.timeout_seconds)
    token = tool_deadline.set(deadline)
    try:
        return jobs.write_input(args, uid('cli_input'))
    finally:
        tool_deadline.reset(token)
