import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from genesisai.core.capabilities.execution import run_tool
from genesisai.core.capabilities.store import CapabilityStore
from genesisai.core.capabilities import tool_results
from genesisai.core.persistence.session import Session
from genesisai.shared.security import Access, ToolError
from genesisai.shared.time_limits import execution_limits


@pytest.fixture
def channel_service(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    session = Session(tmp_path / 'state', workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.commit()
    access = Access([workspace], workspace, session.root, workspace_root=workspace)
    service = SimpleNamespace(store=CapabilityStore(session.root, session.record.project_id),
                              runtime=SimpleNamespace(store=session, access=access))
    return service, workspace


def execute(service, workspace, payload, *, schema=True, code=None, limit=2000000, redaction_secrets=()):
    bundle = workspace / 'bundle'; bundle.mkdir(exist_ok=True)
    program = code if code is not None else (
        "import os,json,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
        "print('diagnostic stdout is not JSON')\nprint('stderr diagnostic', file=sys.stderr)\n"
        + "Path(os.environ['GENESISAI_RESULT_PATH']).write_text(" + repr(json.dumps(payload, ensure_ascii=False)) + ",encoding='utf-8')\n")
    path = bundle / 'tool.py'; path.write_text(program, encoding='utf-8')
    asset = service.store.assets.import_file(path)
    package = {'kind':'tool', 'name':'json_value', 'description':'fixture', 'scope':'project',
               'definition':{'runner':'python','entry':'tool.py','parameters':{'type':'object'},'output_schema':schema},
               'files':{'tool.py':asset}, 'tests':[]}
    item = service.store.create(package)
    return run_tool(package, bundle, {}, workspace, workspace.parent/'logs',
                    owner={key:item[key] for key in ('id','revision','digest')},result_max_bytes=limit, redaction_secrets=redaction_secrets)


@pytest.mark.parametrize('payload', [None, False, 0, [], {}, '', 1.25, ['a', {'b': True}]])
def test_all_json_root_types_survive_separate_result_and_log_channels(channel_service, payload):
    service, workspace = channel_service
    outcome = execute(service, workspace, payload)
    result = tool_results.publish(service, outcome)
    assert result['value_included'] and result['value'] == payload and type(result['value']) is type(payload)
    page = tool_results.read(service, result['result_ref'])
    assert json.loads(page['text']) == payload
    assert 'diagnostic stdout' in tool_results.read(service, result['logs']['stdout'])['text']
    assert 'stderr diagnostic' in tool_results.read(service, result['logs']['stderr'])['text']
    # No dependency on a mutable execution file after successful publication.
    Path(outcome['files']['result']['path']).write_text('changed', encoding='utf-8')
    assert tool_results.publish(service, outcome) == result


def test_large_json_has_exact_paged_result_instead_of_inline_value(channel_service):
    service, workspace = channel_service
    result = tool_results.publish(service, execute(service, workspace, 'x'*100000+'TAIL'))
    assert not result['value_included'] and 'value' not in result
    assert tool_results.read(service, result['result_ref'], offset=100001, limit=4)['text'] == 'TAIL'
    assert tool_results.value(service, result).endswith('TAIL')


@pytest.mark.parametrize('body', ['NaN', 'Infinity', '{broken', '1e1000', '{"a":1,"a":2}'])
def test_invalid_nonfinite_json_cannot_come_from_stdout_fallback(channel_service, body):
    service, workspace = channel_service
    program = "import os\nfrom pathlib import Path\nprint('{}')\nPath(os.environ['GENESISAI_RESULT_PATH']).write_text(" + repr(body) + ")"
    with pytest.raises(ToolError, match='GENESISAI_RESULT_PATH'):
        execute(service, workspace, None, code=program)
    assert list((workspace.parent/'logs').glob('tool_*/channels.json'))


def test_missing_result_has_no_stdout_compatibility(channel_service):
    service, workspace = channel_service
    with pytest.raises(ToolError, match='GENESISAI_RESULT_PATH'):
        execute(service, workspace, None, code="print('{}')")


def test_result_quota_is_independent_of_diagnostic_log_quota(channel_service):
    service, workspace = channel_service
    token = execution_limits.set({'log_max_bytes': 10000})
    try:
        with pytest.raises(ToolError) as failure:
            execute(service, workspace, 'x'*1000, limit=100)
        assert failure.value.error_type == 'result_limit'
    finally:
        execution_limits.reset(token)


@pytest.mark.parametrize('bad_metadata', [{}, {'redacted': False, 'original_bytes': 1}, {'redacted': True, 'original_bytes': True}])
def test_logs_without_execution_redaction_proof_are_not_published(channel_service, bad_metadata):
    service, workspace = channel_service
    outcome = execute(service, workspace, None)
    entry = outcome['files']['stdout']
    entry.pop('redacted')
    entry.pop('original_bytes')
    entry.update(bad_metadata)
    with pytest.raises(ToolError, match='脱敏证明'):
        tool_results.publish(service, outcome)


def test_overlapping_secrets_across_log_chunks_keep_immutable_redacted_metadata(channel_service):
    service, workspace = channel_service
    secret = 'TEST-SECRET-LONG'
    short = 'TEST-SECRET'
    body = 'x' * (1024 * 1024 - 4) + secret + '\n' + short + '\n'
    program = ("import os,sys\nfrom pathlib import Path\n"
        + "sys.stdout.buffer.write(" + repr(body.encode("utf-8")) + ")\n"
        + "Path(os.environ['GENESISAI_RESULT_PATH']).write_text('null', encoding='utf-8')\n")
    outcome = execute(service, workspace, None, code=program, redaction_secrets=(short, secret))
    result = tool_results.publish(service, outcome)
    reference = result['logs']['stdout']
    page = tool_results.read(service, reference, offset=1024 * 1024 - 20, limit=200)
    assert page['redacted'] is True
    assert page['original_bytes'] == len(body.encode('utf-8'))
    assert secret not in page['text'] and short not in page['text']
    assert page['text'].count('[REDACTED]') == 2
    assert '-LONG' not in page['text']
    assert result['value'] is None
    Path(outcome['files']['stdout']['path']).write_text('replaced after publication', encoding='utf-8')
    assert tool_results.read(service, reference, offset=page['offset'], limit=200) == page
    outcome['files']['stdout']['original_bytes'] += 1
    with pytest.raises(ToolError, match='引用发生冲突'):
        tool_results.publish(service, outcome)
