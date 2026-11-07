"""Qwen Token Plan must collect an actual queued tool job to read a random marker."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import secrets
import threading
import time

from docx import Document

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.jobs.storage import Slot
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
    settings = replace(settings, model=args.model, generation={'max_tokens': 2048},
                       context={'max_input_tokens': 100000, 'strategy': 'basic'},
                       request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240})
    model, _ = build_model(settings=settings)
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_async_tools')
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    marker = 'ASYNC_' + secrets.token_hex(8)
    path = workspace / 'document.docx'
    document = Document()
    for i in range(3105): document.add_paragraph('Paragraph ' + str(i))
    document.add_paragraph(marker)
    document.save(path)
    original_hash = sha(path)
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace), providers=[])
    pool = store.root / 'jobs' / 'slots'
    pool.mkdir(parents=True)
    slots = [Slot(pool / f'{i}.lock') for i in range(4)]
    finished = threading.Event()
    def release_slots():
        while not finished.wait(.05):
            if list(runtime.jobs.root.glob('job_*/job.json')):
                finished.wait(7)
                break
        for slot in slots:
            slot.close()
    holder = threading.Thread(target=release_slots, daemon=True)
    holder.start()
    report = {'provider': model.provider, 'model': model.model, 'session_id': store.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    def progress(kind, payload):
        if kind == 'model': print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result':
            print('tool_result', payload.get('call_id'), payload.get('ok'),
                  (payload.get('data') or {}).get('status'), (payload.get('error') or {}).get('code'), flush=True)
    try:
        runner = Runner(model, runtime, seconds=600, max_rounds=14, max_tools=20,
                        token_budget=80000, on_event=progress)
        outcome = RunControl(progress).execute(lambda: runner.start(
            '这是实际异步工具验收。请用docx_read读取工作区document.docx，section=paragraphs，offset=3100，limit=10，'
            '回复最后一段完整的ASYNC_随机验收码及来源。工具若未加载请discover_tools。'
            '执行资源会短暂排队；若返回job_id，请用read_job收取这个作业直到取得实际内容，不能重新提交原操作。'
            '不要联网、修改或删除文件，不要猜测验收码。'))
        calls = list(store.record.calls.values())
        submissions = [item for item in calls if item['call']['name'] == 'docx_read'
                       and (item.get('result', {}).get('data') or {}).get('job_id')]
        job_ids = {item['result']['data']['job_id'] for item in submissions}
        collected = [item for item in calls if item['call']['name'] == 'read_job'
                     and json.loads(item['call']['arguments']).get('job_id') in job_ids
                     and item.get('result', {}).get('ok')
                     and item['result']['data'].get('status') == 'succeeded']
        checks = {'qwen_token_plan': model.provider == 'qwen_token_plan',
                  'completed': outcome['status'] == 'completed', 'queued_submission': bool(submissions),
                  'same_job_collected': bool(collected),
                  'actual_marker_in_result': any(marker in json.dumps(item['result']['data']['result'], ensure_ascii=False) for item in collected),
                  'actual_marker_in_answer': marker in outcome.get('answer', ''),
                  'no_replay': len(submissions) == 1,
                  'source_imported': any(value.get('path') == str(path.resolve()) for value in store.record.sources.values()),
                  'original_preserved': sha(path) == original_hash,
                  'no_pending_jobs': not store.record.run_runtime.get('pending_execution_jobs')}
        report.update(checks=checks, outcome=outcome, passed=all(checks.values()), expected_marker=marker)
    except BaseException as exc:
        runtime.logger.error('live_async_tools_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        finished.set()
        holder.join(2)
        runtime.jobs.stop_all()
        runtime.close()
        report['finished'] = time.time()
        report_path = root / 'report.json'
        report_path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(report_path), 'passed': report['passed'], 'checks': report['checks'],
                          'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
