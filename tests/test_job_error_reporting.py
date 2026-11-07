import io
import json

import pytest

from genesisai.core.jobs import operations, worker
from genesisai.shared.security import ToolError


@pytest.mark.parametrize('operation_fails', [False, True])
def test_publication_and_error_redaction_failures_record_terminal_outcome(tmp_path, monkeypatch, operation_fails):
    calls = []
    def execute(*args):
        calls.append('executed')
        if operation_fails:
            raise ToolError('execution_error', 'private-secret-value')
        return {'value': 'private-secret-value'}
    def broken_clean(value):
        raise TypeError('private-secret-value')
    monkeypatch.setattr(operations, 'execute', execute)
    monkeypatch.setattr(worker, 'save', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('private-secret-value')))
    result = worker.run_operation(
        {'operation': 'builtin_tool', 'data': {}, 'limits': {'result_max_bytes': 10000}},
        tmp_path, {'timeout_seconds': None}, lambda **kwargs: None, broken_clean)
    assert calls == ['executed']
    assert result['status'] == ('failed' if operation_fails else 'unknown')
    assert result['retryable'] is False
    assert 'private-secret-value' not in json.dumps(result)
    assert not (tmp_path / 'result.json').exists()


def test_job_host_ignores_non_string_secret_entries(tmp_path, monkeypatch):
    from genesisai.core.tools import authorization
    root = tmp_path / 'jobs' / 'session' / 'job'
    root.mkdir(parents=True)
    (root / 'job.json').write_text(json.dumps({'kind': 'builtin_tool', 'timeout_seconds': None,
        'status': 'queued'}), encoding='utf-8')
    payload = {'root': str(root), 'operation': 'builtin_tool', 'data': {},
        'limits': {'max_concurrent': 1, 'result_max_bytes': 10000},
        'secrets': [None, {}, [], '', 'private-secret-value']}
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(authorization, 'verify_job_authorization', lambda info: None)
    monkeypatch.setattr(operations, 'execute', lambda *args: (_ for _ in ()).throw(ToolError('execution_error', 'private-secret-value')))
    worker.main()
    info = json.loads((root / 'job.json').read_text(encoding='utf-8'))
    assert info['status'] == 'failed'
    assert info['finished'] >= info['execution_started']
    assert info['error'] == '[REDACTED]'


@pytest.mark.parametrize('executor_result', [{}, {'status': 'running'}, None, []])
def test_host_rejects_nonterminal_executor_result(tmp_path, monkeypatch, executor_result):
    from genesisai.core.tools import authorization
    root = tmp_path / 'jobs' / 'session' / 'job'
    root.mkdir(parents=True)
    (root / 'job.json').write_text(json.dumps({'kind': 'builtin_tool', 'timeout_seconds': None,
        'status': 'queued'}), encoding='utf-8')
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(json.dumps({'root': str(root),
        'limits': {'max_concurrent': 1}, 'secrets': []})))
    monkeypatch.setattr(authorization, 'verify_job_authorization', lambda info: None)
    monkeypatch.setattr(worker, 'run_operation', lambda *args, **kwargs: executor_result)
    worker.main()
    info = json.loads((root / 'job.json').read_text(encoding='utf-8'))
    assert info['status'] == 'unknown'
    assert info['code'] == 'unknown_execution'


def test_atomic_job_record_in_deep_directory(tmp_path):
    from genesisai.core.jobs.storage import save, load
    # Mirrors the nested Skill trial path that used to overflow on Windows.
    root = tmp_path
    while len(str(root)) < 225:
        root /= 'nested'
    root.mkdir(parents=True)
    path = root / 'job.json'
    save(path, {'status': 'running'})
    save(path, {'status': 'succeeded'})
    assert load(path) == {'status': 'succeeded'}
    assert list(root.iterdir()) == [path]
