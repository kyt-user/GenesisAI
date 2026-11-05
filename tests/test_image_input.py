from session_helpers import persisted_sessions
import json

import pytest

from genesisai.app import cli
from genesisai.shared.paths import application_home

from genesisai.shared.messages import Response, image_parts
from test_image_assets import SPRITE
from test_multimodal_runtime import visual_model


@pytest.mark.parametrize('prompt', [None, 'describe picture'])
def test_single_shot_image_flag_reaches_model(tmp_path, monkeypatch, prompt):
    picture = tmp_path / 'sheet.webp'
    picture.write_bytes(SPRITE.read_bytes())
    model = visual_model(Response(content='done'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    args = ['--workspace', str(tmp_path), '--image', str(picture)]
    if prompt:
        args += ['--prompt', prompt]
    assert cli.main(args) == 0
    assert image_parts(model.contexts[0][-1].content)


def test_terminal_attach_clear_and_image_only_send(tmp_path, monkeypatch):
    picture = tmp_path / 'a picture.webp'
    picture.write_bytes(SPRITE.read_bytes())
    model = visual_model(Response(content='done'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    commands = iter([f'/attach "{picture}"', '/attachments', '/attachments clear', f'/attach "{picture}"', '/send', '/history', '/exit'])
    monkeypatch.setattr('rich.console.Console.input', lambda *a, **kw: next(commands))
    assert cli.main(['--workspace', str(tmp_path)]) == 0
    assert len(model.contexts) == 1 and len(image_parts(model.contexts[0][-1].content)) == 1
    data, = persisted_sessions()

    assert data['status'] == 'completed'
    assert 'base64' not in json.dumps(data)


def test_running_cli_queues_frozen_pixels_and_keeps_new_draft_separate(tmp_path, monkeypatch):
    from concurrent.futures import wait
    from threading import Event
    from types import SimpleNamespace
    entered, release = Event(), Event()
    picture = tmp_path / 'queued.webp'
    picture.write_bytes(SPRITE.read_bytes())
    def first(messages):
        entered.set()
        assert release.wait(15)
        return Response(content='first')
    model = visual_model(first, Response(content='second'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'is_interactive', lambda _: True)
    monkeypatch.setattr(cli.onboarding, 'env_key', lambda _: 'fixture')
    idle = iter(['first question', '/exit'])
    busy = iter([f'/attach "{picture}"', 'look at queued image'])
    monkeypatch.setattr(cli.CliView, 'prompt', lambda self: next(idle))
    def running(self, runs, render, draft):
        assert entered.wait(15)
        command = next(busy, None)
        if command:
            return command, ''
        if runs.pending:
            assert len(runs.pending[0].images) == 1
            picture.write_bytes(b'changed after image was queued')
        release.set()
        assert wait([runs.future], timeout=15).done
        return None, ''
    monkeypatch.setattr(cli.CliView, '_get_input', lambda self: SimpleNamespace(read_running=running.__get__(self)))
    try:
        assert cli.main(['--workspace', str(tmp_path)]) == 0
    finally:
        release.set()
    assert len(model.contexts) == 2
    part, = image_parts(model.contexts[-1][-1].content)
    assert model.image_resolver(part).startswith('data:image/png;base64,')
