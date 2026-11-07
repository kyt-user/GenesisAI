"""Protocol identity remains exact even when short credentials occur in IDs."""
import json
import re
from datetime import datetime

from genesisai.core.jobs import operations, worker
from genesisai.core.state.io_journal import IOJournal
from test_file_tool_jobs import runtime, execute


def test_result_protocol_is_not_a_redacted_log(tmp_path, monkeypatch):
    value = {'sha256': 'a' * 64, 'source_refs': ['src_a123'], 'path': 'a/result.json',
             'value': {'declared': 'a'}, 'status': 'accepted'}
    monkeypatch.setattr(operations, 'execute', lambda *args: value)
    result = worker.run_operation(
        {'operation': 'external_tool', 'data': {}, 'limits': {'result_max_bytes': 10000}},
        tmp_path, {'timeout_seconds': None}, lambda **kw: None,
        lambda value: (_ for _ in ()).throw(AssertionError('protocol must not use log cleaner')))
    assert result['status'] == 'succeeded'
    assert json.loads((tmp_path / 'result.json').read_text(encoding='utf-8')) == value


def test_journal_preserves_envelope_and_redacts_short_secret(runtime):
    journal = runtime.logger.journal
    journal.add_secret('a')
    identity = journal.write('payload_test', text='a', path=runtime.store.workspace / 'a')
    value = json.loads(journal.path.read_text(encoding='utf-8').splitlines()[-1])
    assert re.fullmatch('[0-9a-f]{32}', identity)
    assert value['event_id'] == identity and value['session_id'] == runtime.store.id
    assert value['event'] == 'payload_test'
    datetime.fromisoformat(value['timestamp'])
    assert value['payload']['text'] == '[REDACTED]'
    assert '[REDACTED]' in value['payload']['path']


def test_short_secret_does_not_corrupt_job_source_collection(runtime):
    runtime.logger.add_secret('a')
    target = runtime.store.workspace / 'a.txt'
    target.write_text('data', encoding='utf-8')
    result = execute(runtime, 'read_files', {'path': 'a.txt'}, 'call_a')
    assert result['ok'], result
    if result['data'].get('job_id'):
        result = execute(runtime, 'read_job', {'job_id': result['data']['job_id'], 'wait_seconds': 20}, 'collect_a')
        assert result['ok'], result
    assert len(runtime.store.record.sources) == 1
    source = next(iter(runtime.store.record.sources.values()))
    assert source['path'] == str(target)
    from genesisai.shared.ids import sha
    assert source['sha256'] == sha(target)
    info = next(runtime.jobs.root.glob('*/job.json'))
    assert json.loads(info.read_text(encoding='utf-8'))['origin_call']['id'] == 'call_a'
