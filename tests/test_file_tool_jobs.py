"""Actual independent file-tool workers, resource contention and frozen approval."""
import json
from pathlib import Path
import time

import pytest

from genesisai.core.jobs.storage import Slot
from genesisai.core.jobs import tool_operation
from genesisai.core.persistence.session import Session
from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.ids import sha
from genesisai.shared.security import Access


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    result = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                         providers=[], permission_mode='auto')
    try:
        yield result
    finally:
        result.jobs.stop_all()
        result.close()


def execute(runtime, name, args, identity, approved=None):
    return runtime.execute({'id': identity, 'name': name, 'arguments': json.dumps(args)}, approved)


def install_slow(runtime):
    class SlowFileTool:
        def prepare(self, context, args):
            from genesisai.core.extensions.tools.office.helpers import preview_write
            return preview_write(context, args, 'docx')

        def execute(self, context, args):
            from genesisai.core.extensions.tools.office.helpers import docx_create
            time.sleep(2.5)
            return docx_create(context, args)
    original = runtime.registry.get('docx_create')
    runtime.register_tool(ToolDescriptor('slow_document', True, 'office', 'fixture', original.directory, original.spec), SlowFileTool())


def test_long_execution_outlives_foreground_budget_and_reopen_imports_effects(runtime, monkeypatch):
    from docx import Document
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    install_slow(runtime)
    result = execute(runtime, 'slow_document', {'path': 'created.docx', 'paragraphs': ['JOB_TAIL']}, 'submit')
    assert result['ok'], result
    identity = result['data']['job_id']
    assert runtime.jobs.status(identity)['timeout_seconds'] is None
    assert not runtime.store.record.artifacts
    runtime.close()
    store = Session(runtime.store.root, runtime.store.id)
    store.record.answer = 'new host state'
    store.commit()
    reopened = ToolRuntime(store, runtime.access, providers=[], permission_mode='auto')
    try:
        result = execute(reopened, 'read_job', {'job_id': identity, 'wait_seconds': 10}, 'collect')
        assert result['ok'], result
        assert result['data']['status'] == 'succeeded', result
        payload = result['data']['result']
        assert Document(payload['path']).paragraphs[0].text == 'JOB_TAIL'
        assert store.record.answer == 'new host state'
        assert len(store.record.changes) == len(store.record.artifacts) == 1
        assert result['observations'][0]['kind'] == 'resource_changed'
        assert identity not in store.record.run_runtime['pending_execution_jobs']
        again = execute(reopened, 'read_job', {'job_id': identity}, 'again')
        assert again['ok'] and len(store.record.changes) == 1
        assert runtime.store.database.rows('SELECT * FROM index_dirty')
    finally:
        reopened.close()


def test_queued_job_revalidates_approved_source_before_writing(runtime, monkeypatch):
    from docx import Document
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    source = runtime.store.workspace / 'source.docx'
    doc = Document(); doc.add_paragraph('before'); doc.save(source)
    runtime.set_permission('user')
    args = {'path': 'new.docx', 'source': str(source), 'paragraphs': ['addition']}
    pending = execute(runtime, 'docx_create', args, 'create')
    assert pending['pending']
    slots_root = runtime.store.root / 'jobs' / 'slots'
    slots_root.mkdir(parents=True, exist_ok=True)
    slots = [Slot(slots_root / f'{i}.lock') for i in range(4)]
    try:
        submitted = execute(runtime, 'docx_create', args, 'create', runtime.store.record.calls['create']['digest'])
        assert submitted['ok'], submitted
        identity = submitted['data']['job_id']
        assert runtime.jobs.wait(identity, .2)['status'] == 'queued'
        doc.add_paragraph('replaced after approval'); doc.save(source)
    finally:
        for slot in slots: slot.close()
    assert runtime.jobs.wait(identity, 10)['status'] == 'failed'
    result = execute(runtime, 'read_job', {'job_id': identity}, 'collect')
    assert result['error']['code'] == 'confirmation_invalid', result
    assert not (runtime.store.workspace / 'new.docx').exists()


def test_source_registration_is_staged_until_collected(runtime, monkeypatch):
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    path = runtime.store.workspace / 'source.txt'
    path.write_text('page tail', encoding='utf-8')
    result = execute(runtime, 'read_files', {'path': str(path)}, 'read')
    assert result['ok'], result
    identity = result['data']['job_id']
    assert not runtime.store.record.sources
    result = execute(runtime, 'read_job', {'job_id': identity, 'wait_seconds': 10}, 'collect')
    assert result['ok'], result
    assert result['data']['result']['files'][0]['text'] == 'page tail'
    ref = result['source_refs'][0]
    assert runtime.store.record.sources[ref]['sha256'] == sha(path)
    assert result['observations'][0]['kind'] == 'resource_read'


def test_cancel_after_write_preserves_unknown_and_pending(runtime, monkeypatch):
    class WriteThenBlock:
        def prepare(self, context, args): return {'target': str(context.access.write(args['path']))}
        def execute(self, context, args):
            path = context.access.write(args['path'])
            path.write_text('actual side effect', encoding='utf-8')
            time.sleep(60)
            return {'path': str(path)}
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .05)
    spec = ToolSpec(permission='write', side_effect=True, path_scope='output', execution='job',
                    parameters=object_schema(['path'], path={'type': 'string'}))
    runtime.register_tool(ToolDescriptor('write_block', True, 'office', 'fixture', Path('.'), spec), WriteThenBlock())
    result = execute(runtime, 'write_block', {'path': 'partial.txt'}, 'write')
    assert result['ok'], result
    identity = result['data']['job_id']
    deadline = time.monotonic() + 10
    path = runtime.store.workspace / 'partial.txt'
    while not path.exists() and time.monotonic() < deadline: time.sleep(.05)
    assert path.exists()
    stopped = runtime.jobs.stop(identity)
    assert stopped['status'] == 'unknown', stopped
    result = execute(runtime, 'read_job', {'job_id': identity}, 'collect')
    assert result['error']['code'] == 'unknown_execution'
    assert identity in runtime.store.record.run_runtime['pending_execution_jobs']


def test_partial_contract_and_control_tools_cannot_use_file_job_mode():
    from genesisai.core.tools.base import SchemaConfigError
    with pytest.raises(SchemaConfigError):
        ToolSpec(parameters=object_schema([]), permission='core', awaits_input=True, execution='job').validate()
    with pytest.raises(SchemaConfigError):
        ToolSpec(parameters=object_schema([]), permission='read', execution_timeout_seconds=10).validate()
