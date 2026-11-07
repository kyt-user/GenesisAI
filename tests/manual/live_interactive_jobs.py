"""Real Qwen Token Plan roundtrip through a persistent interactive command."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import secrets
import sys
import time

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.ids import uid, sha
from genesisai.shared.run_control import RunControl
from genesisai.shared.security import Access


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--model', default='qwen3.8-flash')
    args = parser.parse_args()
    load_environment()
    settings = load_settings()
    if settings is None or settings.provider != 'qwen_token_plan':
        settings = ProviderSettings(provider='qwen_token_plan', model=args.model)
    model, _ = build_model(settings=replace(settings, model=args.model, generation={'max_tokens': 2048},
        context={'max_input_tokens': 100000, 'strategy': 'basic'},
        request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240}))
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_interactive_jobs')
    workspace = root / 'workspace'; workspace.mkdir(parents=True)
    challenge, marker = 'CHAL_' + secrets.token_hex(8), 'INPUT_' + secrets.token_hex(8)
    path = workspace / 'interactive_fixture.py'
    path.write_text("import sys\nprint('CHALLENGE:' + " + repr(challenge) + ", flush=True)\n"
        + "line=sys.stdin.readline().strip()\n"
        + "if line != " + repr(challenge) + ": raise SystemExit(7)\n"
        + "print('RESULT:' + " + repr(marker) + ", flush=True)\n", encoding='utf-8')
    original_hash = sha(path)
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace), permission_mode='auto'); store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                          providers=[], permission_mode='auto')
    report = {'provider': model.provider, 'model': model.model, 'session_id': store.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    def progress(kind, payload):
        if kind == 'model': print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result': print('tool_result', payload.get('ok'), (payload.get('error') or {}).get('code'), flush=True)
    try:
        command = json.dumps([sys.executable, '-X', 'utf8', '-u', str(path)], ensure_ascii=False)
        prompt = ('这是实际交互式作业验收。先 discover_tools 找 run_commands、write_job_input、read_job。'
            + '请仅启动一次 run_commands，command=' + command
            + '，interaction={"mode":"pipe"}，background=false。程序会等待输入，所以先收取 job_id。'
            + '用 read_job(output=true) 读取 CHALLENGE: 后面的完整随机码，'
            + '再对同一 job_id 调用 write_job_input(action="write",text=该随机码,append_newline=true)，不要重复输入。'
            + '然后用 read_job 读取直到程序成功退出，最终回复 RESULT: 后面的完整随机码并引用 job_id。'
            + '不要读取或修改 fixture 程序文件，不要启动第二个命令，不要联网或猜测输出。')
        runner = Runner(model, runtime, seconds=600, max_rounds=16, max_tools=24, token_budget=90000, on_event=progress)
        outcome = RunControl(progress).execute(lambda: runner.start(prompt))
        calls = list(store.record.calls.values())
        starts = [item for item in calls if item['call']['name'] == 'run_commands' and item.get('result', {}).get('ok')]
        job_ids = {(item['result'].get('data') or {}).get('job_id') for item in starts} - {None}
        inputs = [item for item in calls if item['call']['name'] == 'write_job_input' and item.get('result', {}).get('ok')
                  and json.loads(item['call']['arguments']).get('job_id') in job_ids]
        outputs = [item for item in calls if item['call']['name'] == 'read_job' and item.get('result', {}).get('ok')
                   and (item['result'].get('data') or {}).get('status') == 'succeeded'
                   and json.loads(item['call']['arguments']).get('job_id') in job_ids]
        receipts = [receipt for job_id in job_ids for receipt in (runtime.jobs.root / job_id / 'inputs').glob('input_*.json')]
        answer = outcome.get('answer', '')
        report['checks'] = {'qwen_token_plan': model.provider == 'qwen_token_plan',
            'completed': outcome['status'] == 'completed', 'one_command': len(starts) == 1,
            'same_job_input': len(inputs) == 1, 'one_persisted_input': len(receipts) == 1,
            'input_sent': bool(receipts) and all(json.loads(item.read_text(encoding='utf-8'))['status'] == 'sent' for item in receipts),
            'actual_result_collected': any(marker in item['result']['data'].get('text', '') for item in outputs),
            'actual_result_answer': marker in answer, 'job_cited': any(job_id in answer for job_id in job_ids),
            'original_preserved': sha(path) == original_hash,
            'no_pending_jobs': not store.record.run_runtime.get('pending_execution_jobs')}
        report.update(outcome=outcome, passed=all(report['checks'].values()))
    except BaseException as exc:
        runtime.logger.error('live_interactive_jobs_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        runtime.jobs.stop_all(); runtime.close()
        report['finished'] = time.time()
        target = root / 'report.json'
        target.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(target), 'passed': report['passed'], 'checks': report['checks'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
