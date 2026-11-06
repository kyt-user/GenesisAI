"""无 shell 插值的受控子进程执行。"""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import ExitStack
from threading import Event, Lock, Thread

from genesisai.shared.security import ToolError
from genesisai.shared.ids import uid

from genesisai.shared.run_control import current_run
from genesisai.shared.process_tree import owned_process
from genesisai.core.extensions.tools.shell.commands import process_invocation, validate_command


TOOL_OUTPUT_LIMIT = 12000
LOG_CHUNK_SIZE = 16384


def _capture(context, command, cwd, timeout, log_path):
    control = current_run.get()
    deadline = time.monotonic() + timeout
    logger = context.runtime.logger
    wake = Event()
    write_lock = Lock()
    errors = []
    summaries = {}
    readers = []

    with log_path.open('x', encoding='utf-8', newline='') as log, ExitStack() as cleanup:
        def read(pipe, name):
            summary, size, batch, batch_size = [], 0, [], 0

            def flush():
                nonlocal batch_size
                if batch:
                    text = ''.join(batch)
                    logger.record('command_output_chunk', log_path=str(log_path), stream=name, text=text)
                    batch.clear()
                    batch_size = 0

            try:
                with pipe:
                    for line in pipe:
                        line = logger.journal.clean(line)
                        if size < LOG_CHUNK_SIZE:
                            summary.append(line[:LOG_CHUNK_SIZE - size])
                        size += len(line)
                        with write_lock:
                            log.write(line)
                        if control:
                            control.emit('command_output', {'stream': name, 'text': line})
                        batch.append(line)
                        batch_size += len(line)
                        if batch_size >= LOG_CHUNK_SIZE:
                            flush()
                    flush()
            except Exception as exc:
                errors.append(exc)
            finally:
                summaries[name] = (''.join(summary), size)
                wake.set()

        invocation, env = process_invocation(command, cwd)
        with owned_process(invocation, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            for name in ('stdout', 'stderr'):
                # Reader owns this descriptor until EOF, including bytes left in
                # the pipe after the process tree is terminated.
                pipe = os.fdopen(os.dup(getattr(process, name).fileno()), encoding='utf-8', errors='replace')
                thread = Thread(target=read, args=(pipe, name))
                thread.start()
                readers.append(thread)
                cleanup.callback(thread.join)
            while process.poll() is None or any(thread.is_alive() for thread in readers):
                if errors:
                    raise errors[0]
                if control and control.cancelled.is_set():
                    raise ToolError('command_cancelled', '命令已启动后取消，可能已产生部分修改，请检查输出。')
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                wake.wait(.05)
                wake.clear()
        if errors:
            raise errors[0]
    return process.returncode, summaries


def run_process(context, command, cwd, timeout, *, kind):
    command = validate_command(command)
    deadline = getattr(context.store.record, 'deadline', 0)
    if getattr(context.store.record, 'status') == 'running' and deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ToolError('task_timeout', '任务执行时间已用完，进度已保存')
        timeout = min(timeout, remaining)
    started = time.monotonic()
    log_dir = context.store.root / 'command_logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{uid(kind)}.log"
    logger = context.runtime.logger
    metadata = dict(command=command, cwd=str(cwd), log_path=str(log_path))
    try:
        exit_code, summaries = _capture(context, command, cwd, timeout, log_path)
    except FileNotFoundError as exc:
        logger.error('command_error', exc, **metadata)
        raise ToolError('command_not_found', f'程序 {command[0]!r} 启动失败，工作目录 {cwd}。请检查入口及其解释器是否仍存在。') from exc
    except subprocess.TimeoutExpired as exc:
        logger.error('command_error', exc, timeout=timeout, **metadata)
        raise ToolError('command_timeout', f'命令超过 {timeout} 秒上限', retryable=False) from exc
    except ToolError as exc:
        logger.error('command_error', exc, **metadata)
        raise
    stdout, out_size = summaries['stdout']
    stderr, err_size = summaries['stderr']
    logger.record('command_output', **metadata, exit_code=exit_code, stdout=stdout, stderr=stderr,
                  truncated=max(out_size, err_size) > LOG_CHUNK_SIZE)
    return dict(**metadata, exit_code=exit_code, stdout=stdout[:TOOL_OUTPUT_LIMIT],
                stderr=stderr[:TOOL_OUTPUT_LIMIT], truncated=max(out_size, err_size) > TOOL_OUTPUT_LIMIT,
                elapsed_ms=round((time.monotonic() - started) * 1000), passed=exit_code == 0)
