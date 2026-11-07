"""Background queues, resource contention, invalidation and cancellable scans."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from genesisai.core.jobs.storage import Slot
from genesisai.core.memory.index_scan import IndexConfig, IndexScanner
from genesisai.core.memory.maintenance import Maintenance
from genesisai.core.paths import ApplicationPaths
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.run_control import RunControl, cancellation_requested
from genesisai.shared.security import Access, ToolError


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace), providers=[])
    try:
        yield runtime
    finally:
        runtime.close()


def config(runtime, **settings):
    path = ApplicationPaths(runtime.store.root).project(runtime.store.record.project_id) / 'index.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding='utf-8')


def test_turn_submission_does_not_wait_and_queue_drains_more_than_two(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = runtime.project_index.refresh
    def slow(**options):
        entered.set()
        assert release.wait(10)
        return real(**options)
    monkeypatch.setattr(runtime.project_index, 'refresh', slow)
    started = time.monotonic()
    runtime.prepare_turn()
    assert time.monotonic() - started < 1
    assert entered.wait(5)
    seen = []
    runtime.register_maintenance('fixture', lambda options: seen.append(options['value']) or {'value': options['value']})
    jobs = [runtime.maintenance.enqueue('fixture', {'value': i}) for i in range(6)]
    release.set()
    for identity in jobs:
        assert runtime.maintenance.wait(identity, 10)['state'] == 'completed'
    assert sorted(seen) == list(range(6))
    page = runtime.maintenance.status(limit=2)
    assert len(page['jobs']) == 2 and page['next_offset'] == 2


def test_busy_executor_slots_leave_queue_live_until_capacity_is_available(runtime):
    root = runtime.store.root / 'jobs' / 'slots'
    root.mkdir(parents=True)
    slots = [Slot(root / f'{i}.lock') for i in range(4)]
    runtime.register_maintenance('fixture', lambda options: {'finished': True})
    identity = runtime.maintenance.enqueue('fixture')
    try:
        time.sleep(2.2)
        assert runtime.maintenance.status(identity)['state'] == 'queued'
        assert runtime.maintenance._thread.is_alive()
    finally:
        for slot in slots:
            slot.close()
    assert runtime.maintenance.wait(identity, 5)['state'] == 'completed'


def test_running_cancel_and_shutdown_requeue_survive_reopen(runtime):
    entered = threading.Event()
    def slow(options):
        entered.set()
        while not cancellation_requested():
            time.sleep(.01)
        return {'derived': 'interrupted'}
    runtime.register_maintenance('fixture', slow)
    cancelled = runtime.maintenance.enqueue('fixture', {'test': 'cancel'})
    assert entered.wait(5)
    runtime.maintenance.cancel(cancelled)
    assert runtime.maintenance.wait(cancelled, 5)['state'] == 'cancelled'
    entered.clear()
    resumed = runtime.maintenance.enqueue('fixture', {'test': 'resume'})
    assert entered.wait(5)
    runtime.close()
    assert runtime.maintenance.status(resumed)['state'] == 'queued'
    store = Session(runtime.store.root, runtime.store.id)
    other = ToolRuntime(store, runtime.access, providers=[])
    try:
        other.register_maintenance('fixture', lambda options: {'derived': 'rebuilt'})
        result = other.maintenance.wait(resumed, 5)
        assert result['state'] == 'completed' and result['result']['derived'] == 'rebuilt'
        assert other.maintenance.status(cancelled)['state'] == 'cancelled'
    finally:
        other.close()


def test_cross_instance_cancel_bypasses_active_writer_transaction(runtime):
    entered, observed = threading.Event(), threading.Event()
    def rebuilding(options):
        with runtime.store.database.transaction() as db:
            db.execute('SELECT count(*) FROM files').fetchone()
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not cancellation_requested():
                time.sleep(.01)
            if cancellation_requested():
                observed.set()
        return {'cancel_observed': observed.is_set()}
    runtime.register_maintenance('locked_rebuild', rebuilding)
    identity = runtime.maintenance.enqueue('locked_rebuild')
    assert entered.wait(5)
    started = time.monotonic()
    other = Maintenance(Session(runtime.store.root, runtime.store.id), limits=runtime.jobs._limits)
    status = other.cancel(identity)
    assert time.monotonic() - started < 1
    assert status['cancel_requested'] is True
    assert observed.wait(2)
    assert runtime.maintenance.wait(identity, 5)['state'] == 'cancelled'


def test_submission_during_close_requeues_interrupted_generation(runtime):
    entered, interrupted, release = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    def rebuilding(options):
        attempts.append(options['value'])
        if len(attempts) == 1:
            entered.set()
            while not cancellation_requested():
                time.sleep(.01)
            interrupted.set()
            assert release.wait(5)
        return {'value': options['value']}
    runtime.register_maintenance('fixture', rebuilding)
    old = runtime.maintenance.enqueue('fixture', {'value': 'old'})
    assert entered.wait(5)
    closing = threading.Thread(target=runtime.maintenance.close)
    closing.start()
    try:
        assert interrupted.wait(5)
        new = runtime.maintenance.enqueue('fixture', {'value': 'new'})
        release.set()
        closing.join(3)
        assert not closing.is_alive()
        assert runtime.maintenance.wait(old, 5)['state'] == 'completed'
        assert runtime.maintenance.wait(new, 5)['state'] == 'completed'
        assert attempts.count('old') == 2 and attempts.count('new') == 1
        assert not runtime.maintenance._marker(old).exists()
    finally:
        release.set()
        closing.join(3)


def test_queued_cancel_and_recovery_keep_persisted_signal(runtime, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(runtime.maintenance, 'start', lambda: None)
        identity = runtime.maintenance.enqueue('project_index')
    assert runtime.maintenance.cancel(identity)['state'] == 'cancelled'
    with runtime.store.database.transaction() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (identity,))
    Maintenance.recover_after_host_lock(runtime.store.database)
    assert runtime.maintenance.status(identity)['state'] == 'cancelled'


def test_changes_are_invalidated_before_async_scan_and_stale_scan_cannot_commit(runtime, monkeypatch):
    path = runtime.store.workspace / 'value.py'
    path.write_text('value = 1', encoding='utf-8')
    runtime.project_index.refresh()
    ref = runtime.memory.search('value', corpus='code')[0]['ref']
    note = runtime.memory.write_notes([dict(title='value', kind='architecture', body='one', sources=[ref], status='active')])['entries'][0]
    path.write_text('value = 2', encoding='utf-8')
    with monkeypatch.context() as patch:
        patch.setattr(runtime.maintenance, 'start', lambda: None)
        runtime.deliver_observations([{'kind': 'resource_changed', 'path': str(path)}], call_id='changed')
    assert runtime.memory.read(note['id'])['status'] == 'stale'
    assert runtime.memory.read(ref)['current'] is False
    original_parser = runtime.project_index.parsers['.py']
    def change_during_parse(text):
        path.write_text('value = 3', encoding='utf-8')
        runtime.project_index.mark_changed([path])
        return original_parser(text)
    runtime.project_index.parsers['.py'] = change_during_parse
    partial = runtime.project_index.refresh()
    assert partial['partial'] and partial['changed_during_scan'] == 1
    assert runtime.memory.read(ref)['current'] is False
    runtime.project_index.parsers['.py'] = original_parser
    runtime.maintenance.start()
    for job in runtime.maintenance.status()['jobs']:
        assert runtime.maintenance.wait(job['id'], 5)['state'] == 'completed'
    latest = runtime.memory.search('value', corpus='code')[0]['ref']
    assert runtime.memory.read(latest)['text'] == 'value = 3'


def test_git_path_cache_invalidation_and_explicit_index_scope(runtime):
    root = runtime.store.workspace
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True, capture_output=True)
    (root / 'a.py').write_text('a=1', encoding='utf-8')
    config(runtime, cache_seconds=60)
    scanner = runtime.project_index.scanner
    calls, actual = [], scanner.git
    def observe(args, *positional, **options):
        calls.append(args[0])
        return actual(args, *positional, **options)
    scanner.git = observe
    assert runtime.project_index.refresh()['updated'] == 1
    assert runtime.project_index.refresh()['updated'] == 0
    assert calls.count('ls-files') == 1
    (root / 'b.py').write_text('b=2', encoding='utf-8')
    assert runtime.project_index.refresh()['updated'] == 1
    assert calls.count('ls-files') == 2
    runtime.project_index.refresh(force=True)
    assert calls.count('ls-files') == 3
    (root / 'node_modules').mkdir()
    (root / 'node_modules' / 'c.py').write_text('c=3', encoding='utf-8')
    (root / '.env').write_text('private', encoding='utf-8')
    config(runtime, exclude_directories=[])
    runtime.project_index.refresh(force=True)
    paths = {item['path'] for item in runtime.project_index.overview()}
    assert 'node_modules/c.py' in paths and '.env' not in paths and not any(p.startswith('.git/') for p in paths)


def test_git_wait_timeout_and_cancel_terminate_owned_process(runtime, monkeypatch):
    from genesisai.core.memory import index_scan
    from genesisai.shared.process_tree import owned_process
    processes, entered = [], threading.Event()
    @contextmanager
    def slow_git(command, **options):
        with owned_process([sys.executable, '-c', 'import time; time.sleep(10)'], **options) as process:
            processes.append(process)
            entered.set()
            yield process
    monkeypatch.setattr(index_scan, 'owned_process', slow_git)
    with pytest.raises(ToolError, match='超过配置'):
        runtime.project_index.scanner.git(['ls-files'], IndexConfig(git_timeout_seconds=.2))
    assert processes[-1].poll() is not None
    entered.clear()
    control, errors = RunControl(lambda *args: None), []
    def run():
        try:
            control.execute(lambda: runtime.project_index.scanner.git(['ls-files'], IndexConfig()))
        except ToolError as exc:
            errors.append(exc.error_type)
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    control.cancel()
    thread.join(3)
    assert not thread.is_alive() and errors == ['cancelled']
    assert processes[-1].poll() is not None


def test_cancel_before_scan_cleanup_preserves_unseen_snapshots(runtime):
    root = runtime.store.workspace
    for name in ('a.py', 'b.py'):
        (root / name).write_text('value=1', encoding='utf-8')
    runtime.project_index.refresh()
    (root / 'b.py').unlink()  # This test's generated fixture only.
    control = RunControl(lambda *args: None)
    def progress(state):
        if state['phase'] == 'indexing':
            control.cancel()
    with pytest.raises(ToolError, match='取消'):
        control.execute(lambda: runtime.project_index.refresh(progress=progress))
    assert {row['path'] for row in runtime.project_index.overview()} == {'a.py', 'b.py'}
    runtime.project_index.refresh()
    assert {row['path'] for row in runtime.project_index.overview()} == {'a.py'}


def test_parser_replaced_during_scan_remains_pending_for_next_refresh(runtime):
    (runtime.store.workspace / 'x.custom').write_text('source', encoding='utf-8')
    def replacement(text):
        return {'symbols': [{'name': 'new'}], 'imports': [], 'coverage': 'replacement'}
    def original(text):
        runtime.project_index.register_parser('.custom', replacement)
        return {'symbols': [{'name': 'old'}], 'imports': [], 'coverage': 'original'}
    runtime.project_index.register_parser('.custom', original)
    runtime.project_index.refresh()
    assert runtime.project_index.overview()[0]['coverage'] == 'original'
    assert runtime.project_index.refresh()['updated'] == 1
    assert runtime.project_index.overview()[0]['coverage'] == 'replacement'
