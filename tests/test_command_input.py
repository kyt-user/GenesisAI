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


def run_input(keys, *, palette=False):
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
                    key_bindings=input_bindings(palette=palette)), timeout=3)
            finally:
                await feeder
    return asyncio.run(run())


def test_enter_runs_selected_command():
    assert run_input(['/se', '\r']) == '/sessions'


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
    view.render_startup(snapshot())
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
