from session_helpers import persisted_sessions
from multiprocessing import Pipe, active_children
from threading import Event
import sys

import pytest

from genesisai.app import cli
from genesisai.app.desktop_pet import DesktopPet
from genesisai.shared.messages import Response
from genesisai.shared.paths import application_home

from test_acceptance import FakeModel, call, response


def test_pet_tracks_confirmation_restore_and_cancel(tmp_path, monkeypatch):
    states = []
    monkeypatch.setattr(DesktopPet, 'show', lambda pet, character: states.append(pet.status))
    monkeypatch.setattr(cli, 'select', lambda *args, **kwargs: 1)
    model = FakeModel(response(call('editor', {
        'operation': 'create', 'path': 'approval.txt', 'content': 'pending', 'source_refs': [],
    })))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    assert cli.main(['--workspace', str(tmp_path), '--prompt', 'create file']) == 2
    assert not states
    session, = persisted_sessions()
    commands = iter(['/pet', '/cancel', '/pet', '/new', '/pet', '/exit'])
    monkeypatch.setattr(cli.CliView, 'prompt', lambda self: next(commands))
    assert cli.main(['--session', session['id']]) == 0
    assert states == ['awaiting_confirmation', 'cancelled', 'idle']
    assert not (tmp_path / 'approval.txt').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows desktop pet')
def test_pet_reuses_window_and_reopens_after_close(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    pet = DesktopPet()
    try:
        pet.show(pet.character)
        child, = [p for p in active_children() if p.name == 'GenesisPet']
        pet.show(pet.character)
        assert pet.process is child
        pet.update('awaiting_confirmation')
        pet.show('ray')
        assert pet.character == 'ray' and pet.status == 'awaiting_confirmation'
        assert pet.process is not child
        child = pet.process
        assert len([p for p in active_children() if p.name == 'GenesisPet']) == 1
        child.terminate()
        child.join(timeout=5)
        pet.update('completed')
        pet.show(pet.character)
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

    def show(pet, character):
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
    monkeypatch.setattr(cli, 'select', lambda *args, **kwargs: 1)
    assert cli.main(['--workspace', str(tmp_path)]) == 0
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


def test_bird_gaze_drag_work_and_failure(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from genesisai.app import pet_window
    from PySide6.QtCore import QPoint
    from types import SimpleNamespace

    app = pet_window.QApplication.instance() or pet_window.QApplication([])
    pet = pet_window.PetWindow()
    pet.show()
    app.processEvents()
    center = pet.sprite.mapToGlobal(pet.sprite.rect().center())
    radius = pet.sprite.width() * 3

    for dx, dy, direction in [(radius, 0, 0), (10, -10, 1), (0, -10, 2),
                               (-10, -10, 3), (-10, 0, 4), (-10, 10, 5),
                               (0, 10, 6), (10, 10, 7),
                               (radius, 1, None), (0, 0, None)]:
        cursor = center + QPoint(dx, dy)
        monkeypatch.setattr(pet_window, 'QCursor', SimpleNamespace(pos=lambda: cursor))
        pet.track_cursor()
        assert pet.gaze == direction
        assert pet.current_pose(0) == ((3, direction) if direction is not None else (0, 0))

    pet.gaze = 4
    pet.set_status('running')
    assert pet.current_pose(0) == (4, 0)
    assert pet.current_pose(180) == (4, 1)
    pet.drag_offset = QPoint(20, 20)
    for direction, row in [(-1, 1), (1, 2)]:
        pet.drag_direction = direction
        assert pet.current_pose(0) == (row, 0)
    pet.drag_offset = None
    assert pet.current_pose(0) == (4, 0)
    pet.gaze = None
    for status, row in [('failed', 5), ('awaiting_input', 6),
                        ('awaiting_confirmation', 7), ('partial', 7), ('completed', 8)]:
        pet.set_status(status)
        assert pet.current_pose(0) == (row, 0)
        pet.gaze = 0
        assert pet.current_pose(0) == (row, 0)
    pet.set_status('idle')
    pet.gaze = None
    assert pet.current_pose(0) == (0, 0)
    assert pet.current_pose(5000) == (3, 4)
    assert pet.current_pose(6500) == (3, 0)
    assert [len(row) for row in pet.frames] == [8] * 5 + [1] * 4
    assert all(frame.hasAlphaChannel() and not frame.isNull() for row in pet.frames for frame in row)
    pet.close()


def test_aligned_gaze_and_uninterrupted_jump(monkeypatch):
    """视线切换保持落脚高度；跳跃完整起落，且不会把多帧叠在一起。"""
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from genesisai.app import pet_window
    from PySide6.QtGui import QImage
    from types import SimpleNamespace

    app = pet_window.QApplication.instance() or pet_window.QApplication([])
    pet = pet_window.PetWindow()
    def bounds(frame):
        image = frame.toImage().convertToFormat(QImage.Format_RGBA8888)
        visible = [i for i, a in enumerate(bytes(image.constBits())[3::4]) if a > 128]
        return visible[0] // image.width(), visible[-1] // image.width(), len(visible)

    for row in pet.frames[:4]:
        assert all(bounds(frame)[1] == pet_window.BASELINE for frame in row)

    assert pet.current_pose(1699) == (0, 0)
    assert pet.current_pose(1805) == (0, 1)
    assert pet.current_pose(2539) == (0, 7)
    assert pet.current_pose(2540) == (0, 0)
    assert pet.current_pose(4000) == (0, 0)
    lifts = []
    for elapsed in range(0, 2801, 40):
        pet.elapsed = SimpleNamespace(elapsed=lambda: elapsed)
        pet.draw_frame()
        _, bottom, count = bounds(pet.sprite.pixmap())
        row, column = pet.current_pose(elapsed)
        assert count == bounds(pet.frames[row][column])[2]
        lifts.append(pet_window.BASELINE - bottom)
    assert max(lifts) == pet_window.JUMP_HEIGHT
    assert lifts[-1] == 0
    assert max(abs(b - a) for a, b in zip(lifts, lifts[1:])) <= 5
    for gaze in range(8):
        pet.gaze = gaze
        pet.draw_frame()
        assert bounds(pet.sprite.pixmap())[1] == pet_window.BASELINE
    pet.close()


def test_pet_picker_can_cancel_and_remembers_choice(tmp_path, monkeypatch):
    choices = iter([None, 1, 0])
    selected, defaults = [], []

    def select(console, title, options, *, index):
        assert [option[0] for option in options] == ['黄色毛球', '雷伊']
        defaults.append(index)
        return next(choices)

    def show(pet, character):
        selected.append(character)
        pet.character = character

    commands = iter(['/pet', '/pet', '/pet', '/exit'])
    monkeypatch.setattr(cli, 'select', select)
    monkeypatch.setattr(cli.CliView, 'prompt', lambda self: next(commands))
    monkeypatch.setattr(DesktopPet, 'show', show)
    monkeypatch.setattr(cli, 'build_model', lambda _: pytest.fail('选择桌宠不应启动模型'))
    assert cli.main(['--workspace', str(tmp_path)]) == 0
    assert selected == ['ray', 'bird']
    assert defaults == [0, 0, 1]


def test_ray_original_actions_and_frame_timing(monkeypatch):
    pytest.importorskip('PySide6.QtWidgets')
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from genesisai.app.pet_window import QApplication, RayPetWindow
    from PySide6.QtCore import QPoint

    app = QApplication.instance() or QApplication([])
    ray = RayPetWindow()
    assert [len(row) for row in ray.frames] == [6, 8, 8, 4, 5, 4, 6]
    assert ray.sprite.height() == 130
    start = ray.status_started
    assert ray.current_pose(start + 1899) == (0, 0)
    assert ray.current_pose(start + 1900) == (0, 1)
    ray.set_status('running')
    assert ray.current_pose(ray.status_started) == (6, 0)
    ray.gaze = 0
    assert ray.current_pose(ray.status_started + 105) == (1, 1)
    ray.gaze = 4
    assert ray.current_pose(ray.status_started) == (2, 0)
    ray.drag_offset = QPoint(10, 10)
    ray.drag_direction = 1
    assert ray.current_pose(ray.status_started) == (1, 0)
    ray.drag_offset = ray.gaze = None
    ray.set_status('awaiting_confirmation')
    assert ray.current_pose(ray.status_started) == (3, 0)
    ray.set_status('completed')
    assert ray.current_pose(ray.status_started) == (4, 0)
    assert ray.completion.interval() == sum(ray.delays[4])
    ray.close()
