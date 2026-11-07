"""Local cancellation must preserve unknown external execution outcomes."""
import threading
import time
from types import SimpleNamespace

import pytest

from genesisai.core.jobs.worker import run_operation
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError


@pytest.mark.parametrize('operation', ['mcp_request', 'external_tool', 'builtin_tool'])
@pytest.mark.parametrize('reason', ['stop', 'timeout'])
def test_cancellation_does_not_replace_unknown_side_effect(tmp_path, monkeypatch, operation, reason):
    from genesisai.core.jobs import operations
    entered = threading.Event()

    def execute(*args):
        (tmp_path / 'side_effect.txt').write_text('applied', encoding='utf-8')
        entered.set()
        deadline = time.monotonic() + 5
        while not cancellation_requested() and time.monotonic() < deadline:
            time.sleep(.01)
        assert cancellation_requested()
        raise ToolError('unknown_execution', 'Effect applied; response was lost')

    monkeypatch.setattr(operations, 'execute', execute)

    def cancel():
        assert entered.wait(3)
        (tmp_path / 'stop.json').write_text('{}', encoding='utf-8')

    stopper = threading.Thread(target=cancel) if reason == 'stop' else None
    if stopper:
        stopper.start()
    try:
        result = run_operation(
            {'operation': operation, 'data': {}, 'limits': {'result_max_bytes': 1000}},
            tmp_path, {'timeout_seconds': .01 if reason == 'timeout' else None},
            lambda: None, lambda value: value,
        )
    finally:
        if stopper:
            stopper.join(3)
    assert (tmp_path / 'side_effect.txt').read_text(encoding='utf-8') == 'applied'
    assert result['status'] == 'unknown'
    assert result['code'] == 'unknown_execution'
    assert not (tmp_path / 'result.json').exists()


def test_confirmed_pre_send_cancellation_remains_stopped(tmp_path, monkeypatch):
    from genesisai.core.jobs import operations

    def execute(*args):
        (tmp_path / 'stop.json').write_text('{}', encoding='utf-8')
        while not cancellation_requested():
            time.sleep(.01)
        raise ToolError('cancelled', 'Request cancelled before sending')

    monkeypatch.setattr(operations, 'execute', execute)
    result = run_operation(
        {'operation': 'mcp_request', 'data': {}, 'limits': {'result_max_bytes': 1000}},
        tmp_path, {'timeout_seconds': None}, lambda: None, lambda value: value,
    )
    assert result['status'] == 'stopped'
    assert result['code'] == 'cancelled'


@pytest.mark.parametrize('recorded_status', ['running', 'unknown'])
def test_unknown_job_still_accepts_cancel_without_claiming_it_stopped(tmp_path, recorded_status):
    from genesisai.core.jobs.service import JobService
    from genesisai.core.jobs.storage import load, save
    jobs = JobService(SimpleNamespace(root=tmp_path, id='fixture', record=SimpleNamespace(run_id='run_fixture')), str)
    identity = 'job_unknown_cancel'
    root = jobs._path(identity)
    root.mkdir(parents=True)
    save(root / 'job.json', {'job_id': identity, 'kind': 'command',
                            'status': recorded_status, 'heartbeat': time.time() - 16})
    result = jobs.stop(identity)
    assert result['status'] == 'unknown'
    assert result['cancel_requested'] is True
    assert load(root / 'stop.json')['requested'] > 0
    # The observer must not fabricate an execution acknowledgment in storage.
    assert load(root / 'job.json')['status'] == recorded_status


@pytest.mark.parametrize('operation', ['mcp_request', 'external_tool', 'builtin_tool', 'capability_test', 'capability_try'])
@pytest.mark.parametrize('failure', ['quota', 'disk'])
def test_returned_execution_with_unpublished_result_remains_unknown(tmp_path, monkeypatch, operation, failure):
    from genesisai.core.jobs import operations, worker
    executions = []

    def execute(*args):
        executions.append(operation)
        (tmp_path / 'side_effect.txt').write_text('applied', encoding='utf-8')
        return {'result': 'complete execution result'}

    monkeypatch.setattr(operations, 'execute', execute)
    if failure == 'disk':
        def unavailable(*args, **kwargs):
            raise OSError('fixture result storage unavailable')
        monkeypatch.setattr(worker, 'save', unavailable)
    result = run_operation(
        {'operation': operation, 'data': {}, 'limits': {'result_max_bytes': 5 if failure == 'quota' else 1000}},
        tmp_path, {'timeout_seconds': None}, lambda: None, lambda value: value,
    )
    assert executions == [operation]
    assert (tmp_path / 'side_effect.txt').read_text(encoding='utf-8') == 'applied'
    assert result['status'] == 'unknown'
    assert result['code'] == 'unknown_execution'
    assert '无法发布' in result['error']
    assert not (tmp_path / 'result.json').exists()
