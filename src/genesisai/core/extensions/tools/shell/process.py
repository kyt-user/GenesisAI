"""无 shell 插值的受控子进程执行。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.core.state.store import uid


BLOCKED_EXECUTABLES = frozenset({
    'cmd', 'cmd.exe', 'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe',
    'bash', 'sh', 'zsh', 'rm', 'rmdir', 'del', 'erase', 'format', 'shutdown',
})
BLOCKED_GIT = frozenset({
    'add', 'am', 'apply', 'branch', 'checkout', 'cherry-pick', 'clean', 'clone',
    'commit', 'fetch', 'init', 'merge', 'mv', 'pull', 'push', 'rebase', 'reset',
    'restore', 'revert', 'rm', 'stash', 'switch', 'tag', 'worktree',
})


def validate_command(command):
    if not isinstance(command, list) or not command or len(command) > 64:
        raise ToolError('validation_error', 'command 必须是非空字符串数组且不超过 64 项')
    if any(not isinstance(item, str) or not item or len(item) > 4000 for item in command):
        raise ToolError('validation_error', 'command 参数无效')
    executable = Path(command[0]).name.casefold()
    if executable in BLOCKED_EXECUTABLES:
        raise ToolError('command_forbidden', '不允许通过命令解释器或破坏性程序绕过 Runtime')
    if executable in {'git', 'git.exe'}:
        subcommand = next((item.casefold() for item in command[1:] if not item.startswith('-')), '')
        if subcommand in BLOCKED_GIT:
            raise ToolError('git_write_forbidden', f'V1 不允许 Git 写操作：{subcommand}')
    return command


def safe_environment():
    blocked = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')
    return {key: value for key, value in os.environ.items() if not any(word in key.upper() for word in blocked)}


def run_process(context, command, cwd, timeout, *, kind):
    command = validate_command(command)
    deadline = context.store.data.get('deadline', 0)
    if context.store.data.get('status') == 'running' and deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ToolError('task_timeout', '任务执行时间已用完，进度已保存')
        timeout = min(timeout, remaining)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=safe_environment(),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        context.runtime.logger.error('command_error', exc, command=command, cwd=str(cwd))
        raise ToolError('command_not_found', '命令不存在或不可执行') from exc
    except subprocess.TimeoutExpired as exc:
        decode = lambda value: value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
        context.runtime.logger.error('command_error', exc, command=command, cwd=str(cwd),
                                     stdout=decode(exc.stdout), stderr=decode(exc.stderr), timeout=timeout)
        raise ToolError('command_timeout', f'命令超过 {timeout} 秒上限', retryable=False) from exc
    stdout, stderr = completed.stdout or '', completed.stderr or ''
    context.runtime.logger.record('command_output', command=command, cwd=str(cwd),
                                  exit_code=completed.returncode, stdout=stdout, stderr=stderr)
    full = stdout + ('\n' if stdout and stderr else '') + stderr
    log_dir = context.store.root / 'command_logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{uid(kind)}.log"
    log_path.write_text(context.runtime.logger.journal.clean(full), encoding='utf-8')
    limit = 12000
    result = {
        'command': command,
        'cwd': str(cwd),
        'exit_code': completed.returncode,
        'stdout': stdout[:limit],
        'stderr': stderr[:limit],
        'truncated': len(stdout) > limit or len(stderr) > limit,
        'log_path': str(log_path),
        'elapsed_ms': round((time.monotonic() - started) * 1000),
        'passed': completed.returncode == 0,
    }
    return result



