"""Independent durable-file audit: collection, scope, publication and redaction."""
import json
from pathlib import Path

import pytest

from genesisai.core.jobs import tool_operation
from genesisai.core.persistence.session import Session
from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.security import Access


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                          providers=[], permission_mode='auto')
    try:
        yield runtime
    finally:
        runtime.jobs.stop_all()
        runtime.close()


def execute(runtime, name, arguments, identity):
    return runtime.execute({'id': identity, 'name': name, 'arguments': json.dumps(arguments)})


def test_second_collection_does_not_reemit_applied_change(runtime, monkeypatch):
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    result = execute(runtime, 'docx_create', {'path': 'created.docx', 'paragraphs': ['one']}, 'submit_audit')
    identity = result['data']['job_id']
    first = execute(runtime, 'read_job', {'job_id': identity, 'wait_seconds': 10}, 'collect_first')
    assert first['ok'], first
    assert any(item['kind'] == 'resource_changed' for item in first['observations'])
    second = execute(runtime, 'read_job', {'job_id': identity}, 'collect_second')
    assert second['ok'], second
    assert second['observations'] == []


def test_saved_read_result_survives_original_file_rename(runtime, monkeypatch):
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    path = runtime.store.workspace / 'source.txt'
    path.write_text('saved result', encoding='utf-8')
    result = execute(runtime, 'read_files', {'path': str(path)}, 'submit_read')
    identity = result['data']['job_id']
    assert runtime.jobs.wait(identity, 10)['status'] == 'succeeded'
    path.rename(path.with_name('moved.txt'))  # Only this test's own generated fixture.
    collected = execute(runtime, 'read_job', {'job_id': identity}, 'collect_read')
    assert collected['ok'], collected
    assert collected['data']['result']['files'][0]['text'] == 'saved result'


def test_durable_internal_logs_redact_and_protocol_values_remain_exact(runtime, monkeypatch):
    class EmitSecret:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            from genesisai.shared.ids import sha
            path = context.access.read(args['path'])
            context.runtime.logger.record('fixture_secret', value=args['value'])
            ref = context.store.source(kind='file', path=str(path), sha256=sha(path), title=args['value'])
            return {'source_refs': [ref], 'value': args['value']}

    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    secret = 'AUDIT_OnlyFixture_8c97a67e'
    runtime.logger.journal.add_secret(secret)
    path = runtime.store.workspace / 'source.txt'
    path.write_text('content', encoding='utf-8')
    spec = ToolSpec(execution='job', permission='read', parameters=object_schema(
        ['path', 'value'], path={'type': 'string'}, value={'type': 'string'}))
    runtime.register_tool(ToolDescriptor('emit_secret', True, 'office', 'fixture', Path('.'), spec), EmitSecret())
    result = execute(runtime, 'emit_secret', {'path': str(path), 'value': secret}, 'submit_secret')
    identity = result['data']['job_id']
    assert runtime.jobs.wait(identity, 10)['status'] == 'succeeded'
    root = runtime.jobs._path(identity)
    assert json.loads((root / 'result.json').read_text(encoding='utf-8'))['payload']['value'] == secret
    effects = json.loads((root / 'effects.json').read_text(encoding='utf-8'))
    assert next(iter(effects['sources'].values()))['title'] == secret
    leaked = [str(path.relative_to(root)) for path in root.rglob('*.jsonl')
              if secret in path.read_text(encoding='utf-8')]
    assert not leaked, leaked


def test_collected_directory_listing_checks_reduced_authority(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    outside = tmp_path / 'outside_grants'
    outside.mkdir()
    (outside / 'private_filename.txt').write_text('fixture', encoding='utf-8')
    result = execute(runtime, 'list_files', {'path': str(outside)}, 'submit_outside')
    identity = result['data']['job_id']
    assert runtime.jobs.wait(identity, 10)['status'] == 'succeeded'
    runtime.set_permission('user')
    collected = execute(runtime, 'read_job', {'job_id': identity}, 'collect_outside')
    assert not collected['ok'], collected
    assert collected['error']['code'] in {'permission_denied', 'confirmation_invalid'}


def test_result_quota_after_side_effect_keeps_unknown_and_pending(runtime, monkeypatch):
    class LargeWriteResult:
        def prepare(self, context, args):
            return {'target': str(context.access.write(args['path']))}

        def execute(self, context, args):
            from genesisai.shared.changes import record_change
            from genesisai.shared.ids import sha
            path = context.access.write(args['path'])
            path.write_text('side effect happened', encoding='utf-8')
            record_change(context.store, operation='create', path=path, after_sha=sha(path))
            return {'path': str(path), 'body': 'large structured result' * 1000}

    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    runtime.jobs.settings.parent.mkdir(parents=True, exist_ok=True)
    runtime.jobs.settings.write_text(json.dumps({'result_max_bytes': 200}), encoding='utf-8')
    spec = ToolSpec(execution='job', permission='write', side_effect=True,
                   parameters=object_schema(['path'], path={'type': 'string'}))
    runtime.register_tool(ToolDescriptor('large_write_result', True, 'office', 'fixture', Path('.'), spec), LargeWriteResult())
    result = execute(runtime, 'large_write_result', {'path': 'changed.txt'}, 'submit_large')
    identity = result['data']['job_id']
    status = runtime.jobs.wait(identity, 10)
    collected = execute(runtime, 'read_job', {'job_id': identity}, 'collect_large')
    assert (runtime.store.workspace / 'changed.txt').exists()
    assert status['status'] == 'unknown', {'status': status, 'collected': collected,
        'pending': runtime.store.record.run_runtime.get('pending_execution_jobs'),
        'imported_changes': len(runtime.store.record.changes)}
    assert identity in runtime.store.record.run_runtime['pending_execution_jobs']


def test_interrupted_collector_can_transfer_observations_after_reopen(runtime, monkeypatch):
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    submitted = execute(runtime, 'docx_create', {'path': 'receipt.docx', 'paragraphs': ['one']}, 'receipt_submit')
    identity = submitted['data']['job_id']
    assert runtime.jobs.wait(identity, 10)['status'] == 'succeeded'
    call = {'id': 'interrupted_collect', 'name': 'read_job', 'arguments': json.dumps({'job_id': identity})}
    runtime.ledger.start(call)
    collected = tool_operation.collect_tool(runtime, identity, call['id'])
    assert collected['observations']
    assert runtime.store.record.job_receipts[identity] == call['id']
    # The effect import committed, but the host exited before its call result.
    runtime.close()
    store = Session(runtime.store.root, runtime.store.id)
    assert store.record.calls[call['id']]['state'] == 'unknown'
    reopened = ToolRuntime(store, runtime.access, providers=[], permission_mode='auto')
    try:
        transferred = execute(reopened, 'read_job', {'job_id': identity}, 'resumed_collect')
        assert transferred['ok'], transferred
        assert transferred['observations']
        assert store.record.job_receipts[identity] == 'resumed_collect'
        assert len(store.record.changes) == len(store.record.artifacts) == 1
        repeated = execute(reopened, 'read_job', {'job_id': identity}, 'repeated_collect')
        assert repeated['ok'] and repeated['observations'] == []
    finally:
        reopened.close()
