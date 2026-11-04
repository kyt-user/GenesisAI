from multiprocessing import Pipe, active_children
from threading import Event
import sys

import pytest

from genesisai.app import cli
from genesisai.app.desktop_pet import DesktopPet
from genesisai.shared.messages import Response
from genesisai.core.state.store import application_state_root
from test_acceptance import FakeModel, call, response


def test_pet_tracks_confirmation_restore_and_cancel(tmp_path, monkeypatch):
    states = []
    monkeypatch.setattr(DesktopPet, 'show', lambda pet: states.append(pet.status))
    model = FakeModel(response(call('editor', {
        'operation': 'create', 'path': 'approval.txt', 'content': 'pending', 'source_refs': [],
    })))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    assert cli.main(['--workspace', str(tmp_path), '--prompt', 'create file']) == 2
    assert not states
    session, = (application_state_root() / 'sessions').glob('*.json')
    commands = iter(['/pet', '/cancel', '/pet', '/new', '/pet', '/exit'])
    monkeypatch.setattr(cli.CliView, 'prompt', lambda self: next(commands))
    assert cli.main(['--session', session.stem]) == 0
    assert states == ['awaiting_confirmation', 'cancelled', 'idle']
    assert not (tmp_path / 'approval.txt').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows desktop pet')
def test_pet_reuses_window_and_reopens_after_close(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pet = DesktopPet()
    try:
        pet.show()
        child, = [p for p in active_children() if p.name == 'GenesisPet']
        pet.show()
        assert pet.process is child
        child.terminate()
        child.join(timeout=5)
        pet.update('completed')
        pet.show()
        assert pet.process is not child and pet.process.is_alive()
    finally:
        pet.close()
    assert not [p for p in active_children() if p.name == 'GenesisPet']


def test_pet_command_during_run(tmp_path, monkeypatch):
    release = Event()
    states = []

    def model_reply(_):
        assert release.wait(5)
        return Response(content='done')

    def show(pet):
        states.append(pet.status)
        release.set()

    def busy(view, runs, render):
        if not release.is_set():
            return '/pet'
        runs.future.result(timeout=5)

    commands = iter(['hello', '/exit'])
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'is_interactive', lambda _: True)
    monkeypatch.setattr(cli.onboarding, 'env_key', lambda _: 'fixture')
    monkeypatch.setattr(cli, 'build_model', lambda _: (FakeModel(model_reply), False))
    monkeypatch.setattr(cli.CliView, 'prompt', lambda view: next(commands))
    monkeypatch.setattr(cli.CliView, '_get_input', lambda view: None)
    monkeypatch.setattr(cli.CliView, 'prompt_running', busy)
    monkeypatch.setattr(DesktopPet, 'show', show)
    assert cli.main(['--workspace', str(tmp_path), '--no-agent-docs']) == 0
    assert states == ['running']


def test_pet_window_animation_and_right_click(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from genesisai.app import pet_window

    window_states = []
    parent, child = Pipe()
    parent.send('awaiting_confirmation')
    original = pet_window.PetWindow.set_status

    def record(window, status):
        original(window, status)
        window_states.append((window.status, window.label.text(), window.sprite.pixmap().hasAlphaChannel()))
        if status == 'awaiting_confirmation':
            window.set_status('completed')
            assert window.completion.isActive()
            window.completion.timeout.emit()
            QTimer.singleShot(0, lambda: QTest.mouseClick(window, Qt.RightButton))
        if len(window_states) == 1:
            QTimer.singleShot(3000, window.close)

    monkeypatch.setattr(pet_window.PetWindow, 'set_status', record)
    with parent, child:
        pet_window.run_window(child)
        assert parent.recv() is None
    assert window_states == [('idle', '', True), ('awaiting_confirmation', '等待确认', True),
                             ('completed', '已完成', True), ('idle', '', True)]


def test_mouse_flight_temporarily_overrides_thinking(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from genesisai.app import pet_window
    from PySide6.QtCore import QPoint, Qt
    from types import SimpleNamespace

    app = pet_window.QApplication.instance() or pet_window.QApplication([])
    pet = pet_window.PetWindow()
    pet.show()
    app.processEvents()
    assert pet.label.isHidden()
    pet.set_status('running')
    app.processEvents()
    assert pet.label.isVisible()
    center = pet.sprite.mapToGlobal(pet.sprite.rect().center())
    radius = pet.sprite.width() * 5

    def assert_row(row):
        tile = pet.sheet.copy(0, row * 208, 192, 208)
        expected = tile.scaled(120, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        assert pet.sprite.pixmap().toImage() == expected.toImage()

    for dx, dy, row in [(radius, 0, 1), (10, 0, 1), (-10, 0, 2),
                        (radius, 1, 6), (-1, -radius, 6), (0, 0, 6)]:
        cursor = center + QPoint(dx, dy)
        monkeypatch.setattr(pet_window, 'QCursor', SimpleNamespace(pos=lambda: cursor))
        pet.track_cursor()
        assert_row(row)
        assert pet.label.text() == '正在思考'
    pet.set_status('completed')
    pet.set_flight(1)
    pet.completion.timeout.emit()
    assert pet.label.isHidden()
    assert_row(1)
    pet.close()
