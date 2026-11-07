"""Cross-process host ownership survives delayed heartbeats and CLI reopen."""
import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from genesisai.core.jobs import worker
from genesisai.core.jobs.service import JobService
from genesisai.core.jobs.storage import Slot, save
from genesisai.shared.process_tree import owned_process


def test_live_host_with_stale_heartbeat_is_not_reported_as_unknown(tmp_path):
    store = SimpleNamespace(root=tmp_path / 'state', id='session_fixture')
    service = JobService(store, lambda value: value)
    job_id = 'job_fixture'
    root = service._path(job_id)
    root.mkdir(parents=True)
    save(root / 'job.json', {'status': 'running', 'heartbeat': time.time() - 120})
    code = ("import sys\nfrom genesisai.core.jobs.storage import Slot\n"
            "lease=Slot(sys.argv[1])\nprint('READY',flush=True)\nsys.stdin.read()\nlease.close()\n")
    with owned_process([sys.executable, '-c', code, str(root / 'host.lock')],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        assert child.stdout.readline().strip() == 'READY'
        reopened = JobService(store, lambda value: value)
        status = reopened.status(job_id)
        assert status['status'] == 'running' and status['heartbeat_stale'] is True
        assert not reopened.children  # Ownership does not depend on the originating CLI.
    deadline = time.monotonic() + 5
    while service.status(job_id)['status'] != 'unknown' and time.monotonic() < deadline:
        time.sleep(.02)  # Windows may release a terminated process's byte lock asynchronously.
    assert service.status(job_id)['status'] == 'unknown'


def test_second_host_cannot_execute_or_rewrite_owned_job(tmp_path, monkeypatch):
    save(tmp_path / 'job.json', {'status': 'queued'})
    before = (tmp_path / 'job.json').read_bytes()
    lease = Slot(tmp_path / 'host.lock')
    calls = []
    try:
        monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(json.dumps({'root': str(tmp_path)})))
        monkeypatch.setattr(worker, '_run_host', lambda *args: calls.append('executed'))
        worker.main()
    finally:
        lease.close()
    assert not calls and (tmp_path / 'job.json').read_bytes() == before


@pytest.mark.parametrize('status', ['running', 'succeeded', 'unknown', 'failed'])
def test_restarted_host_does_not_replay_a_started_job(tmp_path, monkeypatch, status):
    save(tmp_path / 'job.json', {'status': status})
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(json.dumps({'root': str(tmp_path)})))
    worker.main()
    assert json.loads((tmp_path / 'job.json').read_text()) == {'status': status}
