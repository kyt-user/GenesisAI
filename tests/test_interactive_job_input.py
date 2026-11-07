"""Actual command pipes/PTYs and cancellation of blocked input transport."""
import json
import os
import sys
import threading
import time

import pytest

from genesisai.core.jobs import input_channel
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline
from test_file_tool_jobs import runtime, execute  # noqa: F401 - shared fixture


def until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('interactive fixture did not become ready')


def start(runtime, code, mode):
    result = execute(runtime, 'run_commands', {'command': [sys.executable, '-X', 'utf8', '-u', '-c', code],
        'background': True, 'interaction': {'mode': mode}}, 'start')
    assert result['ok'], result
    job_id = result['data']['job_id']
    until(lambda: runtime.jobs.status(job_id).get('control_ready'))
    return job_id


def send(runtime, job_id, identity, **args):
    result = execute(runtime, 'write_job_input', {'job_id': job_id, **args}, identity)
    assert result['ok'], result
    return result['data']


def test_pipe_prompts_duplicates_eof_and_secret_redaction(runtime, monkeypatch):
    secret = 'INPUT-SECRET-示例-837'
    monkeypatch.setenv('GENESIS_TEST_INPUT_SECRET', secret)
    code = "import sys\nprint('READY>',end='',flush=True)\ndata=sys.stdin.read()\nprint('BODY:'+data,flush=True)\n"
    job_id = start(runtime, code, 'pipe')
    until(lambda: 'READY>' in runtime.jobs.output(job_id)['text'])
    first = send(runtime, job_id, 'hello', action='write', text='HELLO', append_newline=True)
    repeated = runtime.jobs.write_input({'job_id': job_id, 'action': 'write', 'text': 'HELLO', 'append_newline': True}, 'hello')
    assert repeated['input_id'] == first['input_id']
    sent_secret = send(runtime, job_id, 'secret', action='write', secret_from_env='GENESIS_TEST_INPUT_SECRET', append_newline=True)
    send(runtime, job_id, 'eof', action='close')
    assert runtime.jobs.wait(job_id, 10)['status'] == 'succeeded'
    output = runtime.jobs.output(job_id)['text']
    assert output.count('HELLO') == 1 and secret not in output and '[REDACTED]' in output
    receipt = execute(runtime, 'read_job', sent_secret['read'], 'receipt')
    assert receipt['ok'] and receipt['data']['input']['status'] == 'sent', receipt
    assert secret not in json.dumps(receipt, ensure_ascii=False)


def test_real_pty_resize_input_and_exit(runtime):
    if os.name == 'nt':
        pytest.importorskip('winpty')
    code = "import sys\nprint('TTY:'+str(sys.stdin.isatty()),flush=True)\nprint('READY>',end='',flush=True)\nline=sys.stdin.readline()\nprint('GOT:'+line.strip(),flush=True)\n"
    job_id = start(runtime, code, 'pty')
    until(lambda: 'READY>' in runtime.jobs.output(job_id)['text'])
    assert 'TTY:True' in runtime.jobs.output(job_id)['text']
    resized = send(runtime, job_id, 'resize', action='resize', rows=35, columns=100)
    receipt = execute(runtime, 'read_job', {**resized['read'], 'wait_seconds': 2}, 'resize_receipt')
    assert receipt['ok'] and receipt['data']['input']['status'] == 'sent', receipt
    send(runtime, job_id, 'answer', action='write', text='PTY_OK', append_newline=True)
    state = runtime.jobs.wait(job_id, 10)
    assert state['status'] == 'succeeded', state
    assert 'GOT:PTY_OK' in runtime.jobs.output(job_id)['text']


def test_cancel_before_slow_connection_never_later_commits(monkeypatch):
    release, closed = threading.Event(), threading.Event()
    sent = []
    class Connection:
        def send_bytes(self, data): sent.append(data)
        def close(self): closed.set()
    def connect(*args, **kwargs):
        release.wait(5)
        return Connection()
    monkeypatch.setattr(input_channel, 'Client', connect)
    token = tool_deadline.set(time.monotonic() + .2)
    try:
        with pytest.raises(ToolError) as error:
            input_channel.submit({'address': 'fixture', 'family': 'AF_UNIX', 'key': '00'*32}, {'input_id': 'input_fixture'})
        assert error.value.error_type == 'tool_timeout'
    finally:
        tool_deadline.reset(token)
        release.set()
    assert closed.wait(2) and sent == []


def test_lost_reply_after_commit_is_unknown_and_never_resends(monkeypatch):
    frames, closed = [], threading.Event()
    class Connection:
        def send_bytes(self, value): frames.append(json.loads(value))
        def poll(self, seconds):
            if len(frames) == 1:
                return True
            if closed.wait(min(seconds, .02)):
                raise OSError('closed')
            return False
        def recv_bytes(self): return input_channel.encode({'status': 'ready'})
        def close(self): closed.set()
    monkeypatch.setattr(input_channel, 'Client', lambda *args, **kwargs: Connection())
    token = tool_deadline.set(time.monotonic() + 1)
    try:
        with pytest.raises(ToolError) as error:
            input_channel.submit({'address': 'fixture', 'family': 'AF_UNIX', 'key': '00'*32}, {'input_id': 'input_fixture'})
        assert error.value.error_type == 'unknown_execution'
    finally:
        tool_deadline.reset(token)
    assert frames == [{'input_id': 'input_fixture'}, {'commit': 'input_fixture'}]
    assert closed.is_set()


def test_blocked_pipe_write_is_acknowledged_before_child_reads_and_can_be_stopped(runtime):
    code = "import time\nprint('READY>',end='',flush=True)\ntime.sleep(60)\n"
    job_id = start(runtime, code, 'pipe')
    until(lambda: 'READY>' in runtime.jobs.output(job_id)['text'])
    token = tool_deadline.set(time.monotonic() + 5)
    try:
        receipt = runtime.jobs.write_input({'job_id': job_id, 'action': 'write', 'text': 'x' * (512 * 1024)}, 'blocked_input')
    finally:
        tool_deadline.reset(token)
    assert receipt['status'] in {'accepted', 'sent'}
    state = runtime.jobs.stop(job_id)
    assert state['status'] == 'stopped', state
    final = runtime.jobs.input_receipt(job_id, receipt['input_id'])
    assert final['status'] in {'unknown', 'rejected', 'not_sent'}


def test_json_escaping_does_not_reduce_the_configured_input_byte_quota(runtime):
    from genesisai.core.jobs.storage import save
    runtime.jobs.settings.parent.mkdir(parents=True, exist_ok=True)
    save(runtime.jobs.settings, {'input_max_bytes': 1024})
    code = "import sys\nprint('READY>',end='',flush=True)\nbody=sys.stdin.read()\nprint('COUNT:'+str(len(body)),flush=True)\n"
    job_id = start(runtime, code, 'pipe')
    receipt = runtime.jobs.write_input({'job_id': job_id, 'action': 'write', 'text': '\0' * 1000}, 'escaped_input')
    until(lambda: runtime.jobs.input_receipt(job_id, receipt['input_id'])['status'] == 'sent')
    runtime.jobs.write_input({'job_id': job_id, 'action': 'close'}, 'escaped_eof')
    assert runtime.jobs.wait(job_id, 10)['status'] == 'succeeded'
    assert 'COUNT:1000' in runtime.jobs.output(job_id)['text']


def test_real_pty_echo_redacts_environment_secret(runtime, monkeypatch):
    import hashlib
    if os.name == 'nt':
        pytest.importorskip('winpty')
    secret = 'PTY-密钥-837-test'
    monkeypatch.setenv('GENESIS_TEST_PTY_SECRET', secret)
    code = "import sys,hashlib\nprint('READY>',end='',flush=True)\nline=sys.stdin.readline().strip()\nprint('GOT_SHA:'+hashlib.sha256(line.encode('utf-8')).hexdigest(),flush=True)\n"
    job_id = start(runtime, code, 'pty')
    until(lambda: 'READY>' in runtime.jobs.output(job_id)['text'])
    send(runtime, job_id, 'pty_secret', action='write', secret_from_env='GENESIS_TEST_PTY_SECRET', append_newline=True)
    state = runtime.jobs.wait(job_id, 10)
    assert state['status'] == 'succeeded', state
    output = runtime.jobs.output(job_id)['text']
    assert 'GOT_SHA:' + hashlib.sha256(secret.encode('utf-8')).hexdigest() in output
    assert secret not in output and '[REDACTED]' in output
