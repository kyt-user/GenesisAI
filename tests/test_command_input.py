import asyncio
import socket
from io import StringIO

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from genesisai.app.command_input import CommandCompleter, TaskInput, input_bindings
from genesisai.app.terminal_view import CliView
from test_cli_ui import snapshot

_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Windows asyncio implements its local wake-up socket pair through loopback.
    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('Input tests may only connect to local event-loop sockets')
        return _SOCKET_CONNECT(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)


def test_command_completion_handles_names_and_chinese_without_completing_tasks():
    completer = CommandCompleter()
    def matches(text):
        return [c.text for c in completer.get_completions(Document(text), None)]
    assert '/sessions' in matches('/se')
    assert '/sessions' in matches('/历史')
    assert not matches('please inspect /src')
    assert not matches('/se\nsecond line')
    assert not any(c in matches('/') for c in ('/undo', '/clean', '/doctor'))


def run_input(keys, *, palette=False, recent=False):
    async def run():
        with create_pipe_input() as pipe:
            task_input = TaskInput(input=pipe, output=DummyOutput())
            async def feed():
                for text in keys:
                    await asyncio.sleep(0.05)
                    pipe.send_text(text)
            feeder = asyncio.create_task(feed())
            try:
                return await asyncio.wait_for(task_input.session.prompt_async(
                    '> ', default='/' if palette else '',
                    key_bindings=input_bindings(palette=palette, recent=recent)), timeout=3)
            finally:
                await feeder
    return asyncio.run(run())


def test_enter_runs_selected_command():
    assert run_input(['/se', '\r']) == '/sessions'


def test_home_up_opens_recent_picker():
    assert run_input(['\x1b[A'], recent=True) == '/recent'


def test_tab_completes_without_submitting():
    assert run_input(['/se', '\t', '\r']) == '/sessions'


def test_alt_enter_inserts_newline():
    assert run_input(['first', '\x1b\r', 'second', '\r']) == 'first\nsecond'


def test_bracketed_multiline_paste_waits_for_submit():
    assert run_input(['\x1b[200~first\nsecond\x1b[201~', '\r']) == 'first\nsecond'


def test_escape_closes_command_palette():
    assert run_input(['\x1b'], palette=True) == ''


@pytest.mark.parametrize('width', [48, 80, 120])
def test_compact_header_and_confirmation_render_at_terminal_width(width):
    stream = StringIO()
    view = CliView(Console(file=stream, width=width, force_terminal=False))
    view.render_home(snapshot().workspace, [])
    view.render_confirmation('run_commands', {'target': 'D:/project/workspace', 'command': ['python', '-m', 'unittest']})
    text = stream.getvalue()
    assert 'GenesisAI' in text and 'unittest' in text
    assert text.count('D:/project/workspace') == 2


def test_confirmation_menu_returns_one_frozen_action(monkeypatch):
    import genesisai.app.terminal_view as terminal
    view = CliView(Console(file=StringIO()))
    view.bind_session(snapshot(), {'status': 'awaiting_confirmation', 'pending': [{'id': 'c1'}]})
    monkeypatch.setattr(terminal, 'is_interactive', lambda _: True)
    monkeypatch.setattr(terminal, 'select', lambda *a, **k: 0)
    assert view.prompt() == '/approve'


def test_running_prompt_keeps_editable_draft_across_progress_and_completion():
    from threading import Event
    from genesisai.app.run_queue import RunQueue

    typed = Event()
    observed = []
    runs = RunQueue()
    with create_pipe_input() as pipe:
        editor = TaskInput(input=pipe, output=DummyOutput())
        def action():
            runs.emit('tool_start', {'name': 'run_commands'})
            assert typed.wait(5)
            return {'status': 'completed'}

        def render(kind, payload):
            observed.append(kind)
            pipe.send_text('more')

        def edited(buffer):
            if buffer.text == 'draft more':
                typed.set()

        editor.session.default_buffer.on_text_changed += edited
        try:
            runs.start(action)
            assert editor.read_running(runs, render, 'draft ') == (None, 'draft more')
            assert runs.finish()['status'] == 'completed'
            assert observed == ['tool_start']
        finally:
            typed.set()
            runs.close()


def test_streaming_queue_commits_lines_and_renders_unfinished_line_once():
    from genesisai.app.run_queue import RunQueue

    runs = RunQueue()
    output = []
    def render(kind, payload):
        assert kind == 'stream_token'
        output.append(payload['text'])

    try:
        runs.emit('stream_token', {'text': 'a\nb\nc\nfirst'})
        runs.emit('stream_token', {'text': ' line\nsecond '})
        runs.emit('stream_token', {'text': 'line'})
        runs.drain(render)
        assert output == ['a\nb\nc\nfirst line\n'] and runs.preview == 'second line'
        runs.flush_preview(render)
        runs.flush_preview(render)
        assert ''.join(output) == 'a\nb\nc\nfirst line\nsecond line'
    finally:
        runs.close()


def test_stream_updates_preview_without_erasing_editor(monkeypatch):
    from threading import Event
    from genesisai.app import command_input
    from genesisai.app.run_queue import RunQueue

    handoffs = []
    original = command_input.run_in_terminal
    def handoff(render):
        handoffs.append(True)
        return original(render)
    monkeypatch.setattr(command_input, 'run_in_terminal', handoff)

    runs = RunQueue()
    finished = Event()
    frames = []
    with create_pipe_input() as pipe:
        editor = TaskInput(input=pipe, output=DummyOutput())
        def after_render(app):
            if runs.preview and (not frames or frames[-1] != runs.preview):
                frames.append(runs.preview)
                if len(frames) == 3:
                    finished.set()
                else:
                    runs.emit('stream_token', {'text': '文'})

        editor.session.app.after_render += after_render
        try:
            runs.emit('stream_token', {'text': '中'})
            runs.start(lambda: {'status': 'completed' if finished.wait(4) else 'failed'})
            assert editor.read_running(runs, lambda *_: None, 'draft') == (None, 'draft')
            assert runs.finish()['status'] == 'completed'
            assert frames == ['中', '中文', '中文文']
            assert handoffs == []
        finally:
            finished.set()
            runs.close()
