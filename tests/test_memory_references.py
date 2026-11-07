"""Referenced documents become atomic, revisioned facts without resending bodies."""
import hashlib
import json

import pytest

from genesisai.app.memory_commands import execute_memory_command
from genesisai.core.memory.service import MemoryService
from genesisai.core.persistence.session import Session
from genesisai.core.memory.scope import CodeScope
from test_acceptance import call
from test_memory_paging import runtime, user_source


def entry(title, source_ref, **values):
    return {'title': title, 'kind': 'architecture', 'body_ref': {'ref': source_ref, 'view': 'content'}, **values}


def test_large_document_reference_and_fact_ranges_freeze_complete_body(runtime, tmp_path):
    runtime.set_permission('auto')
    original = 'Design decision.\n' * 15000 + 'TAIL_REQUIREMENT'
    ref = user_source(runtime, original)
    result = runtime.execute(call('memory_write', {'entries': [entry('Whole design', ref),
        entry('Acceptance fact', ref, body_ref={'ref': ref, 'view': 'content',
                                               'offset': len(original)-16, 'length': 16})]}, 'facts'))
    assert result['ok'], result
    notes = result['data']['entries']
    assert len(notes) == 2 and all('body' not in note for note in notes)
    assert notes[0]['body_chars'] == len(original) > 200000
    assert notes[0]['body_reference']['sha256'] == hashlib.sha256(original.encode()).hexdigest()
    restored = MemoryService(Session(runtime.store.root, runtime.store.id))
    first = restored.read(notes[0]['id'])
    second = restored.read(notes[0]['id'], offset=first['next_offset'])
    assert first['text'] + second['text'] == original
    assert first['body_reference']['ref'] == ref
    assert restored.read(notes[1]['id'])['text'] == original[-16:]
    assert notes[0]['sources'] == notes[1]['sources'] == [ref]
    destination = tmp_path / 'knowledge.md'
    restored.export(destination)
    assert original in destination.read_text(encoding='utf-8')


def test_reference_note_version_remains_readable_and_invalidates_transitively(runtime):
    ref = user_source(runtime, 'Original requirement')
    source = runtime.memory.write_notes([entry('Source', ref)])['entries'][0]
    copied = runtime.memory.write_notes([entry('Copied fact', ref,
        body_ref={'ref': source['id'], 'revision': 1}, status='active')])['entries'][0]
    runtime.memory.write_notes([{'ref': source['id'], 'expected_revision': 1, 'title': 'Source',
        'kind': 'architecture', 'body': 'New requirement', 'sources': [ref]}])
    current = runtime.memory.read(copied['id'])
    assert current['status'] == 'stale' and current['revision'] == 2
    assert current['body_reference']['revision'] == 1
    assert current['text'] == runtime.memory.read(copied['id'], revision=1)['text'] == 'Original requirement'
    with pytest.raises(ValueError, match='版本已变化'):
        runtime.memory.write_notes([entry('Old copy', ref, body_ref={'ref': source['id'], 'revision': 1})])


def test_batch_cas_failure_rolls_back_every_fact_and_derived_index(runtime):
    ref = user_source(runtime, 'Evidence')
    a, b = runtime.memory.write_notes([entry('A', ref), entry('B', ref)])['entries']
    with pytest.raises(ValueError, match='记忆版本'):
        runtime.memory.write_notes([
            entry('ROLLBACK_MARKER', ref, ref=a['id'], expected_revision=1),
            entry('conflict', ref, ref=b['id'], expected_revision=99)])
    assert runtime.memory.read(a['id'])['revision'] == runtime.memory.read(b['id'])['revision'] == 1
    assert not runtime.memory.search('ROLLBACK_MARKER', corpus='project')
    assert len(runtime.memory.notes()) == 2
    with pytest.raises(ValueError, match='重复修改'):
        runtime.memory.write_notes([entry('A', ref, ref=a['id'], expected_revision=1)] * 2)


def test_sources_remain_scoped_and_file_scope_cannot_be_bypassed(runtime, tmp_path):
    ref = user_source(runtime, 'Private workspace evidence')
    other_workspace = tmp_path / 'other'
    other_workspace.mkdir()
    other = MemoryService(Session(runtime.store.root, workspace=other_workspace))
    with pytest.raises(ValueError, match='工作区'):
        other.write_notes([entry('Blocked', ref)])
    path = runtime.store.workspace / 'design.md'
    path.write_text('Local design file', encoding='utf-8')
    runtime.project_index.refresh()
    file_ref = runtime.memory.search('Local design', corpus='code')[0]['ref']
    limited = MemoryService(runtime.store, CodeScope(runtime.store.workspace, [runtime.store.workspace / 'allowed']))
    with pytest.raises(ValueError, match='工作区'):
        limited.write_notes([entry('Blocked file', ref, body_ref={'ref': file_ref})])
    note = runtime.memory.write_notes([entry('File fact', ref, body_ref={'ref': file_ref}, status='active')])['entries'][0]
    path.write_text('Changed design file', encoding='utf-8')
    runtime.project_index.refresh()
    assert runtime.memory.read(note['id'])['status'] == 'stale'
    assert runtime.memory.read(note['id'])['text'] == 'Local design file'
    with pytest.raises(ValueError, match='失效来源'):
        runtime.memory.write_notes([entry('Stale active', ref, body_ref={'ref': file_ref}, status='active')])


def test_unbounded_inline_body_and_source_count_keep_evidence_and_redaction(runtime):
    refs = [user_source(runtime, f'evidence {i}') for i in range(21)]
    runtime.logger.add_secret('secret-test-value')
    body = 'Fact text ' * 6000 + 'secret-test-value'
    note = runtime.memory.write_notes([{'title': 'Long fact', 'kind': 'decision', 'body': body,
                                      'sources': refs}])['entries'][0]
    result = runtime.memory.read(note['id'])
    assert len(result['text']) > 32000 and result['sources'] == refs
    assert 'secret-test-value' not in result['text'] and '[REDACTED]' in result['text']
    assert 'body_reference' not in result


def test_batch_cycle_and_body_validation_are_atomic(runtime):
    ref = user_source(runtime, 'Evidence')
    a = runtime.memory.write_notes([entry('A', ref)])['entries'][0]
    b = runtime.memory.write_notes([entry('B', ref, body_ref={'ref': a['id']})])['entries'][0]
    with pytest.raises(ValueError, match='循环'):
        runtime.memory.write_notes([entry('A', ref, ref=a['id'], expected_revision=1,
                                        body_ref={'ref': b['id']})])
    for invalid in ({'title': 'No body', 'kind': 'decision'}, entry('Both', ref, body='text'),
                    entry('Overflow', ref, body_ref={'ref': ref, 'view': 'content', 'offset': 100}),
                    entry('Invalid count', ref, body_ref={'ref': ref, 'view': 'content', 'length': True})):
        with pytest.raises(ValueError):
            runtime.memory.write_notes([entry('Must rollback', ref), invalid])
    assert len(runtime.memory.notes()) == 2


def test_cli_and_tool_accept_only_entries_contract(runtime, tmp_path):
    ref = user_source(runtime, 'CLI evidence')
    path = tmp_path / 'facts.json'
    path.write_text(json.dumps({'entries': [entry('CLI fact', ref)]}), encoding='utf-8')
    result = execute_memory_command(runtime, f'/memory write "{path}"')
    assert len(result['entries']) == 1
    assert runtime.memory.read(result['entries'][0]['id'])['text'] == 'CLI evidence'
    old = {'title': 'old', 'body': 'old', 'kind': 'decision', 'sources': [ref]}
    path.write_text(json.dumps(old), encoding='utf-8')
    with pytest.raises(ValueError, match='entries'):
        execute_memory_command(runtime, f'/memory write "{path}"')
    assert not runtime.execute(call('memory_write', old, 'old_contract'))['ok']


def test_source_revision_race_before_batch_commit_is_rejected(runtime, monkeypatch):
    from genesisai.core.memory import note_writes
    ref = user_source(runtime, 'Source evidence')
    source = runtime.memory.write_notes([entry('Source', ref)])['entries'][0]
    original_prepare = note_writes.prepare
    def race(memory, **values):
        prepared = original_prepare(memory, **values)
        if values['title'] == 'Racing target':
            memory.write_notes([{'ref': source['id'], 'expected_revision': 1, 'title': 'Source',
                'kind': 'architecture', 'body': 'Concurrent correction', 'sources': [ref]}])
        return prepared
    monkeypatch.setattr(note_writes, 'prepare', race)
    with pytest.raises(ValueError, match='来源版本'):
        runtime.memory.write_notes([entry('Racing target', ref, body_ref={'ref': source['id']})])
    assert runtime.memory.read(source['id'])['revision'] == 2
    assert len(runtime.memory.notes()) == 1


def test_cancelled_search_rebuild_preserves_previous_projection(runtime, monkeypatch):
    from genesisai.shared import run_control
    from genesisai.shared.security import ToolError
    ref = user_source(runtime, 'CANCEL_KEEP_MARKER')
    runtime.memory.write_notes([entry('Retained fact', ref)])
    before = runtime.memory.database.rows('SELECT * FROM search_documents ORDER BY id')
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks == 3
    monkeypatch.setattr(run_control, 'cancellation_requested', cancelled)
    with pytest.raises(ToolError, match='已取消'):
        runtime.memory.rebuild_search()
    assert runtime.memory.database.rows('SELECT * FROM search_documents ORDER BY id') == before
    assert runtime.memory.search('CANCEL_KEEP_MARKER')


def test_full_document_remains_searchable_after_rebuild(runtime):
    path = runtime.store.workspace / 'long_design.md'
    path.write_text('design text\n' * 14000 + 'REBUILD_TAIL_REQUIREMENT', encoding='utf-8')
    runtime.project_index.refresh()
    ref = runtime.memory.search('REBUILD_TAIL_REQUIREMENT', corpus='code')[0]['ref']
    runtime.memory.rebuild_search()
    assert runtime.memory.search('REBUILD_TAIL_REQUIREMENT', corpus='code')[0]['ref'] == ref


def test_confirmation_binds_resolved_body_and_source_revision(runtime):
    ref = user_source(runtime, 'Version one')
    source = runtime.memory.write_notes([entry('Source', ref)])['entries'][0]
    pending_call = call('memory_write', {'entries': [entry('Frozen preview', ref,
                        body_ref={'ref': source['id']})]}, 'approval')
    pending = runtime.execute(pending_call)
    assert pending['pending']
    assert pending['preview']['entries'][0]['body_sha256'] == hashlib.sha256(b'Version one').hexdigest()
    digest = runtime.store.record.calls['approval']['digest']
    runtime.memory.write_notes([{'ref': source['id'], 'expected_revision': 1, 'title': 'Source',
        'kind': 'architecture', 'body': 'Version two', 'sources': [ref]}])
    result = runtime.execute(pending_call, approved=digest)
    assert not result['ok'] and result['error']['code'] == 'confirmation_invalid'
    assert len(runtime.memory.notes()) == 1


def test_rebuild_cannot_republish_dirty_file_evidence(runtime):
    path = runtime.store.workspace / 'dirty.py'
    path.write_text('OLD_EVIDENCE = 1', encoding='utf-8')
    runtime.project_index.refresh()
    old = runtime.memory.search('OLD_EVIDENCE', corpus='code')[0]['ref']
    prepared = runtime.memory.prepare_notes([{'title': 'Race fact', 'kind': 'architecture',
        'body_ref': {'ref': old}, 'status': 'active'}])
    path.write_text('NEW_EVIDENCE = 2', encoding='utf-8')
    runtime.project_index.mark_changed(['dirty.py'])
    with pytest.raises(ValueError, match='来源文件已变化'):
        runtime.memory.commit_notes(prepared)
    assert not runtime.memory.search('OLD_EVIDENCE', corpus='code')
    runtime.memory.rebuild_search()
    assert not runtime.memory.search('OLD_EVIDENCE', corpus='code')
    assert runtime.memory.read(old)['current'] is False
    runtime.project_index.refresh(changed_paths=['dirty.py'])
    runtime.memory.rebuild_search()
    assert runtime.memory.search('NEW_EVIDENCE', corpus='code')
