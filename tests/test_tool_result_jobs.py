"""Actual durable Tool results, exact body references and isolation boundaries."""
import json
from copy import deepcopy
from pathlib import Path
import time

import pytest
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.security import Access, ToolError
from test_capability_lifecycle import capability_runtime, tool_package, created, reference, accept, invoke


def finish(runtime, result):
    assert result['ok'], result
    payload = result['data']
    if 'job_id' not in payload:
        return payload
    job_id = payload['job_id']
    deadline = time.monotonic() + 120
    while runtime.jobs.wait(job_id, 5)['status'] in {'queued', 'running', 'stopping'}:
        if time.monotonic() >= deadline:
            runtime.jobs.stop(job_id)
            raise AssertionError('test job did not finish within its explicit budget')
    _, collected = invoke(runtime, 'read_job', {'job_id': job_id})
    assert collected['ok'], collected
    return collected['data']['result']


def test_large_actual_job_refs_are_stable_scoped_and_survive_capability_upgrade(capability_runtime):
    runtime = capability_runtime
    package = tool_package('large_result')
    package['files']['tool.py'] = (
        "import json,os,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
        "print('diagnostic stdout')\nprint('diagnostic stderr', file=sys.stderr)\n"
        "Path(os.environ['GENESISAI_RESULT_PATH']).write_text(json.dumps('x'*100000+'TAIL'),encoding='utf-8')\n")
    package['definition']['output_schema'] = {'type':'string'}
    package['tests'] = [{'name':'long result','input':{'value':1},'expect':{'type':'string','minLength':100004}}]
    item = created(runtime, package)
    _, tested = invoke(runtime, 'test_capability', reference(item))
    assert finish(runtime, tested)['report']['passed']
    accept(runtime, item)
    _, completed = invoke(runtime, 'large_result', {'value':1})
    result = finish(runtime, completed)
    assert result['value_included'] is False and 'value' not in result
    source = runtime.store.record.sources[result['result_ref']]
    _, page = invoke(runtime, 'memory_read', {'ref': result['result_ref'], 'offset':100001, 'limit':4})
    assert page['ok'] and page['data']['text'] == 'TAIL'
    assert runtime.capabilities.collect_tool_result(source['job_id']) == result
    frozen = runtime.capabilities.store.get(item['id'])
    changed = deepcopy(frozen['package']); changed['description'] = 'new revision'
    runtime.capabilities.store.revise(item['id'], item['revision'], changed)
    assert runtime.capabilities.collect_tool_result(source['job_id']) == result
    # Reads use the immutable asset, including after the mutable execution copy changes.
    outcome = runtime.jobs.operation_result(source['job_id'])
    Path(outcome['files']['result']['path']).write_text('changed', encoding='utf-8')
    assert runtime.capabilities.collect_tool_result(source['job_id']) == result
    other = Session(runtime.store.root, workspace=runtime.store.workspace)
    other.record.apply(grants=[str(runtime.store.workspace)], output=str(runtime.store.workspace))
    other.record.sources[result['result_ref']] = source
    other.commit()
    other_runtime = ToolRuntime(other, Access([runtime.store.workspace], runtime.store.workspace, other.root,
                                workspace_root=runtime.store.workspace), providers=[])
    try:
        with pytest.raises(ToolError, match='当前会话'):
            other_runtime.capabilities.read_tool_result(result['result_ref'])
        with pytest.raises(ToolError, match='当前会话'):
            other_runtime.capabilities.read_artifact({'id':source['artifact_id'], 'mode':'text'})
    finally:
        other_runtime.close()


def test_failure_keeps_separate_immutable_log_refs(capability_runtime):
    runtime = capability_runtime
    package = tool_package('failure_logs')
    package['files']['tool.py'] = (
        "import json,os,sys\nfrom pathlib import Path\na=json.load(sys.stdin)\n"
        "print('stdout preserved', flush=True)\nprint('stderr preserved',file=sys.stderr,flush=True)\n"
        "if a['value'] < 0: raise RuntimeError('intentional failure')\n"
        "Path(os.environ['GENESISAI_RESULT_PATH']).write_text(json.dumps({'value':a['value']*2,'cwd':os.getcwd()}))\n")
    item = created(runtime, package)
    _, tested = invoke(runtime, 'test_capability', reference(item))
    assert finish(runtime, tested)['report']['passed']
    accept(runtime, item)
    _, result = invoke(runtime, 'failure_logs', {'value':-1})
    if result['ok'] and 'job_id' in result['data']:
        identity = result['data']['job_id']
        runtime.jobs.wait(identity, 30)
        _, result = invoke(runtime, 'read_job', {'job_id':identity})
    assert not result['ok'] and result['error']['code'] == 'external_tool_failed', result
    for stream in ('stdout','stderr'):
        log_reference = result['data']['logs'][stream]
        _, page = invoke(runtime, 'memory_read', {'ref':log_reference, 'limit':1000})
        assert page['ok'] and stream + ' preserved' in page['data']['text']
        assert page['data']['redacted'] is True
        assert page['data']['original_bytes'] > 0
        assert runtime.store.record.sources[log_reference]['redacted'] is True
