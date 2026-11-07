"""Real independent hosts serialize overlapping writes without blocking unrelated work."""
import json
from pathlib import Path
import sys
import time

from genesisai.core.jobs.storage import Slot
from genesisai.core.jobs.write_coordination import claim, normalize, prepared_paths, release
from test_file_tool_job_audit import runtime, execute


def test_hierarchical_claims_follow_os_owner_and_release_after_owner_death(tmp_path):
    first, second = tmp_path / 'one.lock', tmp_path / 'two.lock'
    owner1, owner2 = Slot(first), Slot(second)
    a, child, other = normalize([tmp_path / 'repo']), normalize([tmp_path / 'repo' / 'x']), normalize([tmp_path / 'other'])
    try:
        assert claim(tmp_path, 'one', first, a)
        assert not claim(tmp_path, 'two', second, child)
        assert claim(tmp_path, 'two', second, other)
        release(tmp_path, 'two')
        owner1.close()
        assert claim(tmp_path, 'two', second, child)
    finally:
        owner1.close(); owner2.close()
    assert prepared_paths({'files': [{'target': str(tmp_path / 'a')}, {'target': str(tmp_path / 'b')}]}, tmp_path) == normalize([tmp_path / 'a', tmp_path / 'b'])


def test_waiting_conflict_does_not_take_slot_and_cancels_before_execution(runtime):
    root = runtime.store.workspace
    runtime.jobs.settings.parent.mkdir(parents=True, exist_ok=True)
    runtime.jobs.settings.write_text(json.dumps({'max_concurrent': 2}))
    gate = root / 'gate'
    first = runtime.jobs.start([sys.executable, '-c', "import time; from pathlib import Path; Path('started').write_text('yes');\nwhile not Path('gate').exists(): time.sleep(.05)"], root, write_paths=[str(root / 'target')])
    end = time.monotonic() + 10
    while not (root / 'started').exists():
        assert time.monotonic() < end
        time.sleep(.05)
    second = runtime.jobs.start([sys.executable, '-c', "from pathlib import Path; Path('must-not-run').write_text('bad')"], root, write_paths=[str(root / 'target' / 'nested')])
    independent = runtime.jobs.start([sys.executable, '-c', "print('independent')"], root, write_paths=[str(root / 'separate')])
    try:
        assert runtime.jobs.wait(independent['job_id'], 10)['status'] == 'succeeded'
        waiting = runtime.jobs.status(second['job_id'])
        assert waiting['status'] == 'queued' and waiting['waiting_for'] == 'conflicting_write'
        runtime.jobs.stop(second['job_id'])
        assert runtime.jobs.wait(second['job_id'], 10)['status'] == 'stopped'
        assert not (root / 'must-not-run').exists()
    finally:
        gate.write_text('done')
        runtime.jobs.stop_all()


def test_queued_editor_revalidates_hash_after_prior_writer(runtime, monkeypatch):
    from genesisai.core.jobs import tool_operation
    from genesisai.shared.ids import sha
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    root = runtime.store.workspace
    path, gate = root / 'source.txt', root / 'gate'
    path.write_text('before\n')
    first = runtime.jobs.start([sys.executable, '-c', "import time; from pathlib import Path; Path('started').write_text('yes');\nwhile not Path('gate').exists(): time.sleep(.05)\nPath('source.txt').write_text('newer\\n')"], root, write_paths=[str(path)])
    end = time.monotonic() + 10
    while not (root / 'started').exists():
        assert time.monotonic() < end
        time.sleep(.05)
    result = execute(runtime, 'editor', {'operation': 'replace', 'path': str(path), 'old_text': 'before', 'new_text': 'obsolete', 'expected_sha256': sha(path)}, 'queued_edit')
    assert result['ok'], result
    identity = result['data']['job_id']
    gate.write_text('go')
    assert runtime.jobs.wait(first['job_id'], 10)['status'] == 'succeeded'
    result = execute(runtime, 'read_job', {'job_id': identity, 'wait_seconds': 10}, 'collect_edit')
    assert not result['ok'] and result['error']['code'] in {'patch_conflict', 'confirmation_invalid'}, result
    assert path.read_text() == 'newer\n'
