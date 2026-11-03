"""只接受当前会话格式，拒绝旧版和损坏数据且不改写原文件。"""

import json

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.state.store import CURRENT_SESSION_SCHEMA_VERSION, Store, atomic_json


def session_path(root, session_id):
    return root / 'sessions' / f'{session_id}.json'


def write_session(root, session_id, data):
    path = session_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def current_session(root):
    return Store(root).data


def test_new_session_has_current_schema_version(tmp_path):
    store = Store(tmp_path / 'work')

    saved = json.loads(store.path.read_text(encoding='utf-8'))

    assert store.data['schema_version'] == CURRENT_SESSION_SCHEMA_VERSION == 3
    assert saved['schema_version'] == 3
    assert saved['tool_runtime'] == {'active_skills': []}


def test_current_session_load_is_idempotent(tmp_path):
    created = Store(tmp_path / 'work')
    created.data['messages'].append({'role': 'user', 'content': 'keep'})
    created.save()

    first = Store(created.root, created.id)
    second = Store(created.root, created.id)

    assert first.data == second.data
    assert second.data['messages'] == [{'role': 'user', 'content': 'keep'}]


@pytest.mark.parametrize('version', [None, 0, 1, 2, 4, True, '3', -1, 1.5, 3.0])
def test_invalid_schema_version_is_rejected_without_rewrite(tmp_path, version):
    root = tmp_path / 'work'
    data = current_session(root)
    if version is None:
        del data['schema_version']
    else:
        data['schema_version'] = version
    path = write_session(root, data['id'], data)
    before = path.read_bytes()

    with pytest.raises(ValueError, match='schema_version'):
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


@pytest.mark.parametrize('field', ['workspace', 'changes', 'permission_modes', 'run_runtime'])
def test_missing_current_fields_are_rejected_without_automatic_repair(tmp_path, field):
    root = tmp_path / 'work'
    data = current_session(root)
    del data[field]
    path = write_session(root, data['id'], data)
    before = path.read_bytes()
    with pytest.raises(ValueError, match=field):
        Store(root, data['id'])
    assert path.read_bytes() == before


def test_unknown_call_remains_blocked_from_replay(tmp_path):
    root = tmp_path / 'work'
    data = current_session(root)
    data['status'] = 'interrupted'
    data['calls'] = {'call_1': {'call': {'id': 'call_1'}, 'state': 'unknown'}}
    write_session(root, data['id'], data)

    loaded = Store(root, data['id'])
    executor = type('Executor', (), {
        'store': loaded, 'composer': None, 'context_budgeter': None,
        'trace': None, 'docs': None, 'logger': loaded.get_logger(),
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

