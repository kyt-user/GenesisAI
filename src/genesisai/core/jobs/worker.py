"""Independent per-job host. The parent may exit without terminating this host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from genesisai.core.jobs.storage import LogWriter, Slot, SlotBusy, save
from genesisai.core.state.io_journal import IOJournal
from genesisai.shared.process_tree import owned_process
from genesisai.shared.run_control import RunControl
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline, execution_limits


def error_text(clean, exc):
    """An error reporter must not lose the job when redaction itself fails."""
    try:
        message = clean(str(exc))
        if isinstance(message, str):
            return message
    except BaseException:
        pass
    return '错误详情无法安全脱敏；请检查作业记录，禁止自动重发'


def run_operation(payload, root, info, publish, clean, *, authorization=None):
    from genesisai.core.jobs.operations import execute
    control = RunControl(lambda event, detail: None)
    completed = threading.Event()
    outcome = {'status': 'unknown', 'code': 'unknown_execution',
               'error': '执行器未发布可验证的完成状态；禁止重发，请核对实际结果',
               'retryable': False}

    def execute_task():
        deadline = time.monotonic() + info['timeout_seconds'] if info['timeout_seconds'] else None
        token = tool_deadline.set(deadline)
        resource_token = execution_limits.set(payload['limits'])
        try:
            value = control.execute(lambda: execute(payload['operation'], payload['data'], root))
            try:
                if not isinstance(value, dict):
                    raise ValueError('作业结果必须为对象')
                # Publishing can fail after an operation has already committed
                # side effects. Never classify that as an unexecuted failure.
                save(root / 'result.json', value, max_bytes=payload['limits']['result_max_bytes'])
            except BaseException as exc:
                raise ToolError('unknown_execution',
                    f'作业执行已返回，但完成结果无法发布；禁止重发，请核对实际结果：{exc}') from exc
            outcome.clear()
            outcome.update(status='succeeded', result_ref=str(root / 'result.json'))
        except BaseException as exc:
            code = getattr(exc, 'error_type', 'job_error')
            uncertain = code == 'unknown_execution' or (payload['operation'] == 'mcp_request'
                and code not in {'validation_error', 'unknown_tool', 'mcp_unavailable', 'cancelled'})
            outcome.update(status='unknown' if uncertain else 'failed', code=code, error=error_text(clean, exc),
                           retryable=bool(getattr(exc, 'retryable', False)))
        finally:
            try:
                tool_deadline.reset(token)
                execution_limits.reset(resource_token)
            finally:
                completed.set()

    thread = threading.Thread(target=execute_task, daemon=True)
    thread.start()
    started = time.monotonic()
    cancelled = None
    while not completed.wait(.1):
        publish()
        if cancelled is None and ((root / 'stop.json').exists() or
                (authorization is not None and authorization.cancelled.is_set())):
            cancelled = ('stopped', time.monotonic())
            control.cancel()
        if cancelled is None and info['timeout_seconds'] and time.monotonic() - started >= info['timeout_seconds']:
            cancelled = ('timed_out', time.monotonic())
            control.cancel()
        if cancelled and time.monotonic() - cancelled[1] >= 5:
            return {'status': 'unknown', 'error': '取消后执行器未退出；禁止重发，请核对实际结果'}
    # Cancellation ends local supervision, not uncertainty about a side effect.
    # Preserve an unknown outcome so collection keeps the reconciliation gate.
    if cancelled and outcome.get('status') != 'unknown':
        outcome['status'] = cancelled[0]
        if authorization is not None and authorization.failure:
            outcome.update(authorization.failure)
    return outcome


def run_command(payload, root, info, publish, clean, *, authorization=None):
    from genesisai.core.jobs.input_channel import InputServer
    from genesisai.core.jobs.job_input import require_current
    from genesisai.core.jobs.stream_output import StreamOutput
    from genesisai.core.jobs.storage import load
    from genesisai.shared.process_tree import safe_environment

    stop = root / 'stop.json'
    writer = LogWriter(root / 'output', payload['limits']['log_max_bytes'])
    channels = {name: LogWriter(root / name, writer.quota) for name in ('stdout', 'stderr')}
    lock = threading.Lock()
    faults, tail = [], []
    interaction = info.get('interaction')
    pty = bool(interaction and interaction['mode'] == 'pty')
    redactor = getattr(clean, '__self__', None)

    def persist(text, name):
        if not text:
            return
        with lock:
            writer.write(text)
            channels[name].write(text)
            tail.append(text[-128_000:])
            while len(tail) > 1 and sum(map(len, tail)) > 128_000:
                tail.pop(0)

    def capture(pipe, name):
        output = StreamOutput(clean)
        try:
            with pipe:
                while chunk := pipe.read1(65536):
                    persist(output.feed(chunk), name)
                persist(output.feed(b'', final=True), name)
        except BaseException as exc:
            faults.append(error_text(clean, exc))

    command = ([sys.executable, '-I', '-X', 'utf8', '-m', 'genesisai.core.jobs.terminal_host']
               if pty else payload['invocation'])
    server, readers = None, []
    status = 'failed'
    with owned_process(command, cwd=info['cwd'], env=safe_environment() if pty else payload['env'],
                       stdin=subprocess.PIPE if interaction else subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            for name in ('stdout', 'stderr'):
                pipe = os.fdopen(os.dup(getattr(process, name).fileno()), 'rb')
                reader = threading.Thread(target=capture, args=(pipe, name), daemon=True)
                reader.start()
                readers.append(reader)
            if pty:
                bridge = {**payload, 'info': info, 'secrets': list(redactor._secrets)}
                process.stdin.write((json.dumps(bridge, ensure_ascii=False) + '\n').encode('utf-8'))
                process.stdin.close()
            elif interaction:
                def validate(request):
                    if stop.exists() or process.poll() is not None or (authorization and authorization.cancelled.is_set()):
                        raise ToolError('job_stopping', '作业正在停止或已经退出，输入未发送')
                    require_current(info, request['authorization_id'])
                def write(text):
                    data = text.encode('utf-8')
                    if process.stdin.write(data) != len(data):
                        raise ToolError('unknown_execution', '管道输入未确认完整发送；禁止重发')
                    process.stdin.flush()
                def resize(rows, columns):
                    raise ToolError('validation_error', '只有 PTY 作业可以调整终端尺寸')
                server = InputServer(root, payload['control'], payload['limits']['input_max_bytes'],
                    write=write, close_input=process.stdin.close, resize=resize,
                    validate=validate, redactor=redactor).start()
                publish(force=True, control_ready=True)
            started = time.monotonic()
            while process.poll() is None:
                changes = {'log_bytes': writer.size, 'process_id': process.pid}
                if pty and (root / 'control.ready').is_file():
                    changes['control_ready'] = True
                publish(**changes)
                if stop.exists() or (authorization is not None and authorization.cancelled.is_set()):
                    status = 'stopped'
                    break
                if server and server.fault:
                    faults.append(server.fault)
                if faults:
                    status = 'resource_exhausted'
                    break
                if info['timeout_seconds'] is not None and time.monotonic() - started >= info['timeout_seconds']:
                    status = 'timed_out'
                    break
                time.sleep(.05)
            else:
                status = 'succeeded' if process.returncode == 0 else 'failed'
        finally:
            if server:
                server.close()
    for reader in readers:
        reader.join()
    result = {'status': 'resource_exhausted' if faults else status, 'exit_code': process.returncode,
              'log_bytes': writer.size, 'control_ready': False}
    if faults:
        result['error'] = faults[0]
    elif status == 'stopped' and authorization is not None and authorization.failure:
        result.update(authorization.failure)
    if pty and status in {'succeeded', 'failed'} and not faults:
        try:
            terminal = load(root / 'terminal.json')
        except (OSError, ValueError):
            terminal = {'status': 'unknown', 'code': 'unknown_execution', 'error': '终端退出结果不可核实；禁止重发'}
        if terminal['status'] == 'exited':
            result.update(exit_code=terminal['exit_code'],
                          status='succeeded' if terminal['exit_code'] == 0 else 'failed')
        else:
            result.update({key: value for key, value in terminal.items() if key in {'status', 'code', 'error'}})
            if result['status'] not in {'failed', 'unknown'}:
                result.update(status='unknown', code='unknown_execution', error='终端退出结果未确认；禁止重发')
    if info.get('verification'):
        result['passed'] = result['status'] == 'succeeded' and result['exit_code'] == 0
    if info.get('framework'):
        from genesisai.core.extensions.tools.shell.python_support import parse_test_summary
        summary = parse_test_summary(info['framework'], ''.join(tail), '', result['exit_code'])
        result.update(summary=summary, passed=result['status'] == 'succeeded' and summary['passed'] > 0 and not summary['failed'] and not summary['errors'])
    return result


def main():
    payload = json.load(sys.stdin)
    root = Path(payload["root"])
    try:
        lease = Slot(root / 'host.lock')
    except SlotBusy:
        return  # An existing host owns this job; never overwrite or replay it.
    try:
        _run_host(payload, root)
    finally:
        lease.close()


def _run_host(payload, root):
    info = json.loads((root / "job.json").read_text(encoding="utf-8"))
    if info['status'] != 'queued':
        return  # A restart is observation/reconciliation, not execution replay.
    redactor = object.__new__(IOJournal)
    redactor._secrets = {value for value in payload.pop("secrets", [])
                         if isinstance(value, str) and value}
    slot, last_write = None, 0
    claimed = False

    def publish(force=False, **changes):
        nonlocal last_write
        info.update(changes, heartbeat=time.time(), host_pid=os.getpid())
        if force or time.monotonic() - last_write >= .5:
            save(root / "job.json", info)
            last_write = time.monotonic()

    from genesisai.core.jobs.authorization_watch import AuthorizationWatch
    authorization = AuthorizationWatch(info)
    try:
        authorization.start()
        pool = root.parent.parent / "slots"
        pool.mkdir(parents=True, exist_ok=True)
        while slot is None:
            if (root / "stop.json").exists():
                publish(force=True, status="stopped", finished=time.time())
                return
            if authorization.cancelled.is_set():
                publish(force=True, status="failed", finished=time.time(), **authorization.failure)
                return
            if info.get("write_paths") and not claimed:
                from genesisai.core.jobs.write_coordination import claim, normalize
                claimed = claim(info["state_root"], info["job_id"], root / "host.lock", normalize(info["write_paths"]))
                if not claimed:
                    publish(status="queued", waiting_for="conflicting_write")
                    time.sleep(.1)
                    continue
            for index in range(payload["limits"]["max_concurrent"]):
                try:
                    slot = Slot(pool / f"{index}.lock")
                    break
                except SlotBusy:
                    continue
            publish(status="queued")
            if slot is None:
                time.sleep(.1)
        publish(force=True, status="running", waiting_for=None, execution_started=time.time())
        if (root / 'stop.json').exists():
            publish(force=True, status='stopped', finished=time.time())
            return
        from genesisai.core.tools.authorization import verify_job_authorization
        verify_job_authorization(info)
        result = (run_command if info['kind'] == 'command' else run_operation)(
            payload, root, info, publish, redactor.clean, authorization=authorization)
        if not isinstance(result, dict) or result.get('status') not in {'succeeded', 'failed', 'stopped', 'timed_out', 'resource_exhausted', 'unknown'}:
            raise ToolError('unknown_execution', '执行器未返回终态；禁止重发，请核对实际结果')
        if authorization.failure:
            result['authorization_failure'] = authorization.failure['code']
        publish(force=True, **result, finished=time.time())
    except BaseException as exc:
        code = getattr(exc, 'error_type', 'job_error')
        publish(force=True, status='unknown' if code == 'unknown_execution' else 'failed', code=code,
                error=error_text(redactor.clean, exc), finished=time.time())
    finally:
        authorization.close()
        if slot:
            slot.close()
        if claimed and info["status"] != "unknown":
            from genesisai.core.jobs.write_coordination import release
            release(info["state_root"], info["job_id"])


if __name__ == "__main__":
    main()
