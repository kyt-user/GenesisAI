import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from genesisai.core.jobs.service import JobService


def store(root):
    return SimpleNamespace(root=Path(root), id="durable", record=SimpleNamespace(run_id="run_test"))


def test_job_snapshot_read_retries_transient_sharing_lock(tmp_path, monkeypatch):
    from genesisai.core.jobs.storage import save, load
    path = tmp_path / 'job.json'
    save(path, {'status': 'succeeded', 'job_id': 'job_once'})
    original, attempts = Path.open, []
    def shared_open(self, *args, **kwargs):
        if self == path:
            attempts.append(self)
            if len(attempts) == 1:
                raise PermissionError(13, 'Sharing violation')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', shared_open)
    assert load(path) == {'status': 'succeeded', 'job_id': 'job_once'}
    assert len(attempts) == 2


def test_actual_submitter_exit_preserves_execution_and_full_paged_output(tmp_path):
    script = """
import json,sys
from pathlib import Path
from types import SimpleNamespace
from genesisai.core.jobs.service import JobService
s=SimpleNamespace(root=Path(sys.argv[1]),id='durable',record=SimpleNamespace(run_id='run_test'))
j=JobService(s,str)
result=j.start([sys.executable,'-X','utf8','-u','-c',"import time; time.sleep(.8); print('开始'+'x'*1200000+'尾部')"],sys.argv[1])
print(result['job_id'],flush=True)
"""
    parent = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)], capture_output=True, text=True, check=True)
    job_id = parent.stdout.strip()
    jobs = JobService(store(tmp_path), str)
    assert jobs.wait(job_id, 12)["status"] == "succeeded"
    cursor, pages = 0, []
    while True:
        result = jobs.output(job_id, cursor, 8000)
        pages.append(result["text"])
        cursor = result["next_offset"]
        if not result["more"]:
            break
    expected = "开始" + "x" * 1200000 + "尾部" + ("\r\n" if sys.platform == "win32" else "\n")
    assert "".join(pages) == expected
    assert cursor == result["output_bytes"]
    assert len(list((jobs.root / job_id / "output").glob("*.log"))) == 2


def test_queue_cancellation_and_detach_do_not_replay(tmp_path):
    jobs = JobService(store(tmp_path), str)
    jobs.settings.parent.mkdir(parents=True)
    jobs.settings.write_text(json.dumps({"max_concurrent": 1}), encoding="utf-8")
    first = jobs.start([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"], tmp_path)["job_id"]
    second = None
    try:
        deadline = time.monotonic() + 5
        while jobs.status(first)["status"] == "queued" and time.monotonic() < deadline:
            time.sleep(.05)
        second = jobs.start([sys.executable, "-c", "from pathlib import Path; Path('must_not_execute').write_text('bad')"], tmp_path)["job_id"]
        assert jobs.wait(second, .5)["status"] == "queued"
        jobs.close()
        restored = JobService(store(tmp_path), str)
        assert restored.stop(second)["status"] == "stopped"
        assert restored.stop(first)["status"] == "stopped"
        assert not (tmp_path / "must_not_execute").exists()
        assert restored.status(limit=1)["more"]
        assert len(restored.status(offset=1, limit=1)["jobs"]) == 1
    finally:
        jobs.stop(first)
        if second:
            jobs.stop(second)


def test_explicit_log_quota_is_a_visible_failure_and_preserves_output(tmp_path):
    jobs = JobService(store(tmp_path), str)
    jobs.settings.parent.mkdir(parents=True)
    jobs.settings.write_text(json.dumps({"log_max_bytes": 2000}), encoding="utf-8")
    job = jobs.start([sys.executable, "-u", "-c", "print('before'); print('x'*3000)"], tmp_path)["job_id"]
    result = jobs.wait(job, 5)
    assert result["status"] == "resource_exhausted"
    assert "配额" in result["error"]
    assert "before" in jobs.output(job)["text"]
