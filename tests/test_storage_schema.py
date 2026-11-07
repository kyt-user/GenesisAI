"""Transactional current-format storage and immutable history contracts."""

import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from genesisai.core.persistence.database import Database, SCHEMA_VERSION, encode
from genesisai.core.persistence.session import Session, SessionConflict
from genesisai.core.persistence.files import atomic_json


def test_new_session_has_database_schema_and_no_json_session(tmp_path):
    session = Session(tmp_path / "app", workspace=tmp_path / "work")
    assert (
        session.database.rows("PRAGMA user_version")[0]["user_version"]
        == SCHEMA_VERSION
    )
    assert not (session.root / "sessions").exists()
    assert session.record.tool_runtime == {"active_skills": {}}


def test_load_is_idempotent_and_history_is_immutable(tmp_path):
    session = Session(tmp_path / "app", workspace=tmp_path / "work")
    session.record.messages.append({"role": "user", "content": "keep"})
    session.commit()
    first = Session(session.root, session.id)
    second = Session(session.root, session.id)
    assert first.record == second.record
    first.record.messages[0]["content"] = "overwrite"
    with pytest.raises(ValueError, match="cannot be overwritten"):
        first.commit()
    assert Session(session.root, session.id).record.messages[0]["content"] == "keep"


def test_concurrent_writer_does_not_lose_messages(tmp_path):
    first = Session(tmp_path / "app", workspace=tmp_path / "work")
    second = Session(first.root, first.id)
    first.record.messages.append({"role": "user", "content": "winner"})
    first.commit()
    second.record.messages.append({"role": "user", "content": "stale"})
    with pytest.raises(SessionConflict):
        second.commit()
    assert Session(first.root, first.id).record.messages == first.record.messages


def test_initialization_is_serialized(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: Database(tmp_path).path, range(8)))
    assert len(set(paths)) == 1


def test_unknown_database_version_rejected_without_rewrite(tmp_path):
    database = Database(tmp_path)
    with database.transaction() as db:
        db.execute("PRAGMA user_version=99")
    before = database.path.read_bytes()
    with pytest.raises(ValueError, match="version"):
        Database(tmp_path)
    assert database.path.read_bytes() == before


def test_corrupt_state_is_not_repaired(tmp_path):
    session = Session(tmp_path / "app", workspace=tmp_path / "work")
    with session.database.transaction() as db:
        db.execute("UPDATE sessions SET state=? WHERE id=?", ("{", session.id))
    before = session.database.path.read_bytes()
    with pytest.raises(ValueError):
        Session(session.root, session.id)
    assert session.database.path.read_bytes() == before


def test_started_call_recovers_unknown_and_cannot_start_new_run(tmp_path):
    from genesisai.agent.loop import Runner

    session = Session(tmp_path / "app", workspace=tmp_path / "work")
    session.record.status = "running"
    session.record.calls["call_1"] = {"call": {"id": "call_1"}, "state": "started"}
    session.commit()
    recovered = Session(session.root, session.id)
    assert recovered.record.calls["call_1"]["state"] == "unknown"
    assert recovered.record.status == "interrupted"
    with pytest.raises(ValueError):
        Runner.check_available(recovered.record)


def test_atomic_json_export_uses_replace(tmp_path, monkeypatch):
    import genesisai.core.persistence.files as storage

    calls = []
    real = storage.os.replace

    def replace(source, target):
        calls.append((source, target))
        real(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    path = tmp_path / "export.json"
    atomic_json(path, {"value": 1})
    assert len(calls) == 1 and calls[0][1] == path
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads(path.read_text()) == {"value": 1}
