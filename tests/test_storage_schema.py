"""会话 JSON Schema 版本、迁移和损坏保护。"""

import json

import pytest

from genesisai.agent.runner import Runner
from genesisai.core.state.store import CURRENT_SESSION_SCHEMA_VERSION, Store, atomic_json


def session_path(root, session_id):
    return root / 'sessions' / f'{session_id}.json'


def write_session(root, session_id, data):
    path = session_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def legacy_session(session_id='session_legacy'):
    return dict(
        id=session_id,
        status='idle',
        messages=[{'role': 'user', 'content': '保留原文'}],
        calls={'call_1': {'call': {'id': 'call_1'}, 'state': 'succeeded'}},
        sources={'src_1': {'kind': 'web', 'title': '来源'}},
        artifacts={'artifact_1': {'path': 'result.md', 'sha256': 'abc', 'run_id': 'run_1'}},
        pending=[{'id': 'call_pending'}],
        grants=['D:/materials'],
        output='D:/output',
        remote_allowed=True,
        run_id='run_1',
        rounds=2,
        tool_count=3,
        failures=0,
        deadline=123.5,
        usage={'total_tokens': 42},
        history=[{'run_id': 'run_0', 'status': 'completed'}],
    )


def test_new_session_has_current_schema_version(tmp_path):
    store = Store(tmp_path / 'work')

    saved = json.loads(store.path.read_text(encoding='utf-8'))

    assert store.data['schema_version'] == CURRENT_SESSION_SCHEMA_VERSION == 3
    assert saved['schema_version'] == 3
    assert saved['tool_runtime'] == {'active_skills': []}


def test_unversioned_session_migrates_without_losing_content(tmp_path):
    root = tmp_path / 'work'
    original = legacy_session()
    path = write_session(root, original['id'], original)

    loaded = Store(root, original['id'])
    saved = json.loads(path.read_text(encoding='utf-8'))

    assert loaded.data['schema_version'] == saved['schema_version'] == 3
    assert loaded.data['tool_runtime'] == {'active_skills': []}
    for key, value in original.items():
        assert loaded.data[key] == value
        assert saved[key] == value


def test_v1_session_migrates_to_v2_without_losing_content(tmp_path):
    root = tmp_path / 'work'
    original = legacy_session()
    original['schema_version'] = 1
    path = write_session(root, original['id'], original)

    loaded = Store(root, original['id'])
    saved = json.loads(path.read_text(encoding='utf-8'))

    assert loaded.data['schema_version'] == saved['schema_version'] == 3
    assert loaded.data['tool_runtime'] == saved['tool_runtime'] == {'active_skills': []}
    for key, value in original.items():
        if key != 'schema_version':
            assert loaded.data[key] == saved[key] == value


def test_current_session_load_is_idempotent(tmp_path):
    created = Store(tmp_path / 'work')
    created.data['messages'].append({'role': 'user', 'content': 'keep'})
    created.save()

    first = Store(created.root, created.id)
    second = Store(created.root, created.id)

    assert first.data == second.data
    assert second.data['messages'] == [{'role': 'user', 'content': 'keep'}]


@pytest.mark.parametrize('version', [True, '1', -1, 1.5])
def test_invalid_schema_version_is_rejected_without_rewrite(tmp_path, version):
    root = tmp_path / 'work'
    data = legacy_session()
    data['schema_version'] = version
    path = write_session(root, data['id'], data)
    before = path.read_bytes()

    with pytest.raises(ValueError, match='schema_version'):
        Store(root, data['id'])

    assert path.read_bytes() == before


def test_future_schema_version_is_rejected_without_rewrite(tmp_path):
    root = tmp_path / 'work'
    data = legacy_session()
    data['schema_version'] = CURRENT_SESSION_SCHEMA_VERSION + 1
    path = write_session(root, data['id'], data)
    before = path.read_bytes()

    with pytest.raises(ValueError, match='高于当前支持版本'):
        Store(root, data['id'])

    assert path.read_bytes() == before


@pytest.mark.parametrize('payload', ['{', '[]', '"text"', 'null'])
def test_broken_or_non_object_json_is_not_overwritten(tmp_path, payload):
    root = tmp_path / 'work'
    path = session_path(root, 'session_broken')
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding='utf-8')
    before = path.read_bytes()

    with pytest.raises(ValueError):
        Store(root, 'session_broken')

    assert path.read_bytes() == before


def test_migrated_unknown_call_remains_blocked_from_replay(tmp_path):
    root = tmp_path / 'work'
    data = legacy_session()
    data['status'] = 'interrupted'
    data['calls'] = {'call_1': {'call': {'id': 'call_1'}, 'state': 'unknown'}}
    write_session(root, data['id'], data)

    loaded = Store(root, data['id'])
    executor = type('Executor', (), {
        'store': loaded, 'composer': None, 'context_budgeter': None,
        'trace': None, 'docs': None,
    })()

    assert loaded.data['schema_version'] == 3
    assert loaded.data['calls']['call_1']['state'] == 'unknown'
    assert Runner(None, executor).resume()['status'] == 'interrupted'


def test_atomic_json_uses_replace_and_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / 'session.json'
    calls = []
    import genesisai.core.state.store as storage

    real_replace = storage.os.replace

    def recording_replace(source, target):
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(storage.os, 'replace', recording_replace)

    atomic_json(path, {'schema_version': 2})

    assert len(calls) == 1
    assert calls[0][1] == path
    assert not path.with_suffix('.tmp').exists()

