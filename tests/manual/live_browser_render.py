"""Real Qwen API + real Chromium; synthetic HTTPS content is pinned and deterministic."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import secrets
import socket
import time
from urllib.parse import urlsplit

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.ids import uid
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
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_browser')
    workspace = root / 'workspace'; workspace.mkdir(parents=True)
    marker = 'RENDER_' + secrets.token_hex(8)
    log = root / 'network.jsonl'
    def resolver(*a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    def transport(url, ip, headers):
        with log.open('a') as stream: stream.write(json.dumps(url) + '\n')
        if urlsplit(url).path == '/article':
            return 200, {'content-type': 'text/html'}, b'<html><body><article id="article">Loading</article><script src="/app.js"></script></body></html>'
        if urlsplit(url).path == '/app.js':
            script = 'setTimeout(()=>{document.querySelector("#article").innerHTML="<p id=ready>Actual dynamic content. ' + marker + '. This evidence was generated after a browser executed JavaScript.</p>"},1000)'
            return 200, {'content-type': 'application/javascript'}, script.encode()
        raise AssertionError('Unexpected request')
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace)); store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
        network=Network(resolver=resolver, transport=transport), providers=[], permission_mode='auto')
    report = {'provider': model.provider, 'model': model.model, 'session_id': store.id, 'started': time.time(), 'passed': False, 'checks': {}}
    def progress(kind, payload):
        if kind == 'model': print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result': print('tool_result', payload.get('ok'), (payload.get('error') or {}).get('code'), flush=True)
    try:
        outcome = RunControl(progress).execute(lambda: Runner(model, runtime, seconds=600, max_rounds=16, max_tools=24,
            token_budget=90000, on_event=progress).start(
                '这是动态网页读取验收。请 discover_tools 找到 render_web_page，然后读取 https://public.test/article，'
                'wait_for="#ready"，settle_seconds=6。网页由实际Chromium执行JavaScript，内容来自隔离测试服务。'
                '若返回job_id，使用read_job收取同一个作业，禁止重复渲染。最终回复页面中的完整RENDER_随机码并引用来源。'
                '随机码不会预先告诉你。不要使用命令、创建或修改文件，不需要创建能力。'))
        calls = list(store.record.calls.values())
        submitted = [item for item in calls if item['call']['name'] == 'render_web_page' and item.get('result', {}).get('ok')]
        jobs = {(item['result'].get('data') or {}).get('job_id') for item in submitted} - {None}
        collected = [item for item in calls if item['call']['name'] == 'read_job' and item.get('result', {}).get('ok')
            and json.loads(item['call']['arguments']).get('job_id') in jobs and item['result']['data'].get('status') == 'succeeded']
        checks = {'qwen_token_plan': model.provider == 'qwen_token_plan', 'completed': outcome['status'] == 'completed',
            'one_render': len(submitted) == 1, 'job_returned': bool(jobs), 'same_job_collected': bool(collected),
            'actual_marker': marker in outcome.get('answer', ''),
            'render_source_saved': any(value['kind'] == 'web_render' for value in store.record.sources.values()),
            'only_fixture_requests': len(log.read_text().splitlines()) == 2,
            'no_pending_jobs': not store.record.run_runtime.get('pending_execution_jobs')}
        report.update(checks=checks, outcome=outcome, passed=all(checks.values()))
    except BaseException as exc:
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
