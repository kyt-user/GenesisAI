"""Durable suite recovery never reruns completed or uncertain cases implicitly."""
import json
from pathlib import Path
import time

import pytest

from genesisai.core.jobs.storage import Slot, load
from genesisai.shared.security import ToolError
from test_capability_lifecycle import capability_runtime, created, tool_package, reference, invoke
from test_tool_result_jobs import finish


def checkpoint(runtime, report_id):
    return runtime.capabilities.store.root / 'tests' / report_id / 'suite.json'


def test_actual_cancel_resume_requires_decision_and_reuses_completed_cases(capability_runtime):
    runtime = capability_runtime
    marker = runtime.store.workspace / 'executions.jsonl'
    gate = runtime.store.workspace / 'allow-second'
    package = tool_package()
    package['files']['tool.py'] = (
        "import json, os, sys, time\nfrom pathlib import Path\nargs=json.load(sys.stdin)\n"
        + 'with Path(' + repr(str(marker)) + ").open('a') as stream: stream.write(str(args['value'])+'\\n')\n"
        + 'while args["value"] == 2 and not Path(' + repr(str(gate)) + ').exists(): time.sleep(.05)\n'
        + "json.dump({'value':args['value']*2,'cwd':os.getcwd()},open(os.environ['GENESISAI_RESULT_PATH'],'w'))\n")
    package['tests'] = [{'name': 'case_' + str(i), 'input': {'value': i}, 'expect': {'type': 'object', 'properties': {'value': {'const': i * 2}}}} for i in (1, 2, 3)]
    item = created(runtime, package)
    _, started = invoke(runtime, 'test_capability', reference(item))
    assert started['ok'] and started['data']['status'] == 'running', started
    job_id = started['data']['job_id']
    progress = runtime.jobs.output(job_id)['progress']
    report_id = progress['report_id']
    args = {**reference(item), 'resume_report_id': report_id}
    with pytest.raises(ToolError) as busy:
        runtime.capabilities._test_now(args)
    assert busy.value.error_type == 'suite_busy'
    assert marker.read_text().splitlines() == ['1', '2']
    runtime.jobs.stop(job_id)
    end = time.monotonic() + 10
    while runtime.jobs.status(job_id)['status'] in {'running', 'stopping'}:
        assert time.monotonic() < end
        time.sleep(.05)
    state = load(checkpoint(runtime, report_id))
    assert state['completed'] == 1 and state['running']['name'] == 'case_2'
    with pytest.raises(ToolError) as unclear:
        runtime.capabilities._test_now(args)
    assert unclear.value.error_type == 'reconciliation_required'
    assert marker.read_text().splitlines() == ['1', '2']
    gate.write_text('allowed')
    _, resumed = invoke(runtime, 'test_capability', {**args, 'reconcile': {'case': 'case_2', 'action': 'retry'}})
    result = finish(runtime, resumed)
    assert result['report']['passed'] and result['report']['id'] == report_id
    assert marker.read_text().splitlines() == ['1', '2', '2', '3']
    assert result['report']['reconciliations'][0]['action'] == 'retry'
    completed = runtime.capabilities._test_now(args)
    assert completed['report']['passed'] and marker.read_text().splitlines() == ['1', '2', '2', '3']


def test_checkpoint_cannot_be_reused_for_another_revision_or_path(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    result = runtime.capabilities._test_now(reference(item))
    args = {**reference(item), 'resume_report_id': result['report']['id']}
    other = created(runtime, tool_package('other'))
    with pytest.raises(ToolError) as changed:
        runtime.capabilities._test_now({**reference(other), 'resume_report_id': args['resume_report_id']})
    assert changed.value.error_type == 'revision_conflict'
    with pytest.raises(ToolError) as invalid:
        runtime.capabilities._test_now({**reference(item), 'resume_report_id': '../other'})
    assert invalid.value.error_type == 'invalid_report'


def test_mark_unknown_failed_never_passes_or_repeats_it(capability_runtime, monkeypatch):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    original = runtime.capabilities._trial
    def interrupted(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(runtime.capabilities, '_trial', interrupted)
    with pytest.raises(KeyboardInterrupt):
        runtime.capabilities._test_now(reference(item))
    path = next((runtime.capabilities.store.root / 'tests').glob('test_*/suite.json'))
    state = load(path)
    monkeypatch.setattr(runtime.capabilities, '_trial', original)
    result = runtime.capabilities._test_now({**reference(item), 'resume_report_id': state['report_id'],
        'reconcile': {'case': 'double', 'action': 'mark_failed'}})
    assert not result['report']['passed']
    assert result['report']['cases'][0]['code'] == 'execution_unknown'
    assert not (Path(state['running']['workspace']) / 'output.txt').exists()
    assert result['report']['cases'][1]['passed']
