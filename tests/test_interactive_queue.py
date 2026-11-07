from session_helpers import persisted_sessions
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from genesisai.app import cli
from genesisai.core.extensions.tools.shell.process import _capture
from genesisai.shared.paths import application_home

from genesisai.model.providers.qwen import QwenClient
from genesisai.shared.messages import Response
from genesisai.shared.run_control import RunControl, current_run
from genesisai.shared.security import ToolError
from test_acceptance import FakeModel, call, response


@pytest.mark.parametrize('outcome', ['completed', 'partial', 'failed', 'cancelled', 'approval',
                                     'registration_failed', 'after_registration_failed'])
def test_cli_dispatches_fifo_after_persisted_outcome(tmp_path, monkeypatch, outcome):
    entered, release = Event(), Event()

    def first(_):
        entered.set()
        assert release.wait(8)
        if outcome == 'failed':
            raise ValueError('fixture failure')
        if outcome == 'approval':
            return response(call('editor', {'operation': 'create', 'path': 'approved.txt',
                                            'content': 'allowed', 'source_refs': []}))
        return Response(content='[[PARTIAL]]first' if outcome == 'partial' else 'first')

    if outcome in {'registration_failed', 'after_registration_failed'}:
        original = cli.Session.commit
        saves = []
        def save(store):
            if store.record.messages[-1:] and store.record.messages[-1]['content'] == 'message two':
                saves.append(1)
                fail_at = 1 if outcome == 'registration_failed' else 2
                if len(saves) == fail_at:
                    raise OSError('fixture session write failure')
            return original(store)
        monkeypatch.setattr(cli.Session, 'commit', save)
    responses = [first]
    if outcome == 'approval':
        responses.append(Response(content='approved result'))
    if outcome != 'after_registration_failed':
        responses.append(Response(content='second'))
    model = FakeModel(*responses, Response(content='third'))
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'is_interactive', lambda _: True)
    monkeypatch.setattr(cli.onboarding, 'env_key', lambda provider: 'fixture')
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    busy_commands = iter(['/status', 'message two', 'message three', '/queue'] +
                         (['/cancel'] if outcome == 'cancelled' else []))
    idle_commands = iter(['message one'] + (
        ['/approve'] if outcome == 'approval' else
        ['/queue resume'] if outcome != 'completed' else []) + ['/exit'])
    work = tmp_path / 'work'

    def saved():
        data, = persisted_sessions()
        return data

    def idle(view):
        cmd = next(idle_commands)
        if cmd in {'/queue resume', '/approve'}:
            state = saved()
            expected = {'approval': 'awaiting_confirmation', 'registration_failed': 'completed',
                        'after_registration_failed': 'failed'}
            assert state['status'] == expected.get(outcome, outcome)
            users = ['message one', 'message two'] if outcome == 'after_registration_failed' else ['message one']
            assert [m['content'] for m in state['messages'] if m['role'] == 'user'] == users
        if cmd == '/exit':
            assert view.draft == 'unfinished draft'
        view.draft = ''
        return cmd

    def busy(self, runs, render, draft):
        assert entered.wait(8)
        cmd = next(busy_commands, None)
        if cmd:
            if cmd == '/queue':
                assert [item.text for item in runs.pending] == ['message two', 'message three']
                assert len(model.contexts) == 1
            return cmd, ''
        release.set()
        assert wait([runs.future], timeout=8).done
        return None, 'unfinished draft'

    monkeypatch.setattr(cli.CliView, 'prompt', idle)
    monkeypatch.setattr(cli.CliView, '_get_input', lambda self: SimpleNamespace(read_running=busy.__get__(self)))
    assert cli.main(['--workspace', str(work)]) == 0
    final = saved()
    assert [m['content'] for m in final['messages'] if m['role'] == 'user'] == ['message one', 'message two', 'message three']
    assert final['status'] == 'completed' and final['answer'] == 'third'
    assert (work / 'approved.txt').exists() == (outcome == 'approval')
    journal = application_home() / 'logs' / (final['id'] + '.jsonl')
    events = [json.loads(line) for line in journal.read_text(encoding='utf8').splitlines()]
    added = {e['payload']['queue_id']: e['payload']['text'] for e in events if e['event'] == 'queue_add'}
    starts = [e for e in events if e['event'] == 'queue_start']
    assert [added[e['payload']['queue_id']] for e in starts] == ['message two', 'message three']
    assert [e['run_id'] for e in starts] == [final['history'][1]['run_id'], final['run_id']]
    assert all(e['run_id'] is None for e in events if e['event'] in {'queue_add', 'queue_dispatch'})


_CONNECT = socket.socket.connect


def test_cli_exit_recovers_worker_before_closing_session(tmp_path, monkeypatch):
    entered = Event()

    def blocking(_):
        entered.set()
        assert current_run.get().cancelled.wait(5)
        raise KeyboardInterrupt

    model = FakeModel(blocking)
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'is_interactive', lambda _: True)
    monkeypatch.setattr(cli.onboarding, 'env_key', lambda provider: 'fixture')
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    monkeypatch.setattr(cli.CliView, 'prompt', lambda self: 'start')
    commands = iter(['next', '/queue clear', 'later', '/exit'])

    def read_running(*args):
        assert entered.wait(5)
        return next(commands), ''

    monkeypatch.setattr(cli.CliView, '_get_input', lambda self: SimpleNamespace(read_running=read_running))
    assert cli.main(['--workspace', str(tmp_path / 'work')]) == 0
    state, = persisted_sessions()
    assert state['status'] == 'cancelled'
    assert [m['content'] for m in state['messages'] if m['role'] == 'user'] == ['start']
    events = [json.loads(line) for line in (application_home() / 'logs' / (state['id'] + '.jsonl')).read_text(
        encoding='utf8').splitlines()]
    discarded, = [e for e in events if e['event'] == 'queue_discard']
    assert discarded['payload']['count'] == 1
    assert events[-1]['event'] == 'session_close'


@pytest.mark.parametrize('send_headers', [False, True])
def test_cancellation_closes_actual_model_socket(monkeypatch, send_headers):
    def local_only(sock, address):
        assert address[0] in ('127.0.0.1', '::1')
        return _CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', local_only)
    waiting, disconnected = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if send_headers:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n')
                self.wfile.flush()
            waiting.set()
            self.connection.settimeout(8)
            if self.connection.recv(1) == b'':
                disconnected.set()

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        control = RunControl(lambda *args: None)
        model = QwenClient('fixture', api_key='fixture', base_url=f'http://127.0.0.1:{server.server_port}')
        try:
            with ThreadPoolExecutor(1) as worker:
                saw_chunk = Event()
                def consume():
                    for item in model.stream_chat([]):
                        saw_chunk.set()
                future = worker.submit(control.execute, consume)
                reached_server = waiting.wait(8)
                if not reached_server and future.done():
                    future.result()  # Surface a client failure instead of a misleading server wait assertion.
                assert reached_server
                if send_headers:
                    assert saw_chunk.wait(3)
                control.cancel()
                with pytest.raises(KeyboardInterrupt):
                    future.result(timeout=3)
                assert disconnected.wait(3)
        finally:
            control.cancel()
            server.shutdown()
            thread.join()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object')
def test_windows_cancel_reaps_grandchild_after_launcher_exits(tmp_path):
    import ctypes
    from ctypes.wintypes import HANDLE
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = HANDLE
    live = Event()
    child = []
    def emit(kind, item):
        if item['stream'] == 'stdout':
            child.append(kernel.OpenProcess(0x100000, False, int(item['text'])))
            live.set()

    control = RunControl(emit)
    context = SimpleNamespace(runtime=SimpleNamespace(logger=SimpleNamespace(
        journal=SimpleNamespace(clean=str), record=lambda *args, **kwargs: None)))
    script = 'import os,time; print(os.getpid(), flush=True); time.sleep(30)'
    launcher = f'import subprocess,sys; subprocess.Popen([sys.executable, "-c", {script!r}])'
    with ThreadPoolExecutor(1) as worker:
        future = worker.submit(control.execute, lambda: _capture(
            context, [sys.executable, '-c', launcher], tmp_path, 8, tmp_path / 'command.log'))
        try:
            assert live.wait(5) and child[0]
        finally:
            control.cancel()
        with pytest.raises(ToolError, match='取消'):
            future.result(timeout=3)
        try:
            assert kernel.WaitForSingleObject(HANDLE(child[0]), 1000) == 0
        finally:
            kernel.CloseHandle(HANDLE(child[0]))


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object')
def test_windows_job_reaps_tree_when_host_is_killed(tmp_path):
    import ctypes
    from ctypes.wintypes import HANDLE

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = HANDLE
    child = 'import os,time; print(os.getpid(),flush=True); time.sleep(8)'
    launcher = ('import subprocess,sys,os,time; print(os.getpid(),flush=True); '
                f'subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(8)')
    owner = ('import sys,subprocess,time; sys.path.insert(0,sys.argv[1]); '
             'from genesisai.shared.process_tree import owned_process; '
             'job=owned_process([sys.executable,"-c",sys.argv[2]]); job.__enter__(); time.sleep(9)')
    src = Path(__file__).resolve().parents[1] / 'src'
    handles = []
    with subprocess.Popen([sys._base_executable, '-c', owner, str(src), launcher],
                          stdout=subprocess.PIPE, text=True, cwd=tmp_path) as host:
        try:
            for _ in range(2):
                handle = kernel.OpenProcess(0x100000, False, int(host.stdout.readline()))
                assert handle
                handles.append(handle)
            host.kill()
            host.wait(timeout=3)
            assert all(kernel.WaitForSingleObject(HANDLE(h), 2000) == 0 for h in handles)
        finally:
            host.kill()
            for handle in handles:
                kernel.CloseHandle(HANDLE(handle))
