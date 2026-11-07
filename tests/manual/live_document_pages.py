"""Qwen Token Plan acceptance using only a generated long DOCX fixture."""
import argparse
from dataclasses import replace
import json
import re
from pathlib import Path
import secrets
import time

from docx import Document
from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.ids import uid
from genesisai.shared.security import Access


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--model', default='qwen3.8-flash')
    args = parser.parse_args()
    load_environment()
    saved = load_settings()
    if saved is None or saved.provider != 'qwen_token_plan':
        saved = ProviderSettings(provider='qwen_token_plan', model=args.model)
    model, _ = build_model(settings=replace(saved, model=args.model, generation={'max_tokens': 2048},
        context={'max_input_tokens': 100000, 'strategy': 'basic'},
        request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240}))
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_document_pages')
    workspace = root / 'workspace'; workspace.mkdir(parents=True)
    marker = 'MARKER_' + secrets.token_hex(8)
    path = workspace / 'long.docx'
    doc = Document()
    for i in range(3105): doc.add_paragraph('Paragraph ' + str(i))
    doc.add_paragraph(marker); doc.save(path)
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace)); store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace), providers=[])
    report = {'provider': model.provider, 'model': model.model, 'session_id': store.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    def progress(kind, payload):
        if kind == 'model': print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result': print('tool_result', payload.get('ok'), flush=True)
    try:
        result = Runner(model, runtime, seconds=600, max_rounds=10, max_tools=12,
                        token_budget=60000, on_event=progress).start(
            '这是长DOCX分页验收。请实际调用docx_read读取工作区long.docx，section=paragraphs，offset=3100，limit=10。'
            '如果工具尚未加载先discover_tools。回复末尾一段原样MARKER_验收码，可附必要来源引用；不要猜测、联网或修改文件。')
        calls = [entry for entry in store.record.calls.values() if (entry.get('call') or {}).get('name') == 'docx_read']
        checks = report['checks']
        checks['completed'] = result['status'] == 'completed'
        checks['actual_tail_page'] = any(entry.get('result', {}).get('ok') and
            json.loads(entry['call']['arguments']).get('offset') == 3100 and
            any(item.get('text') == marker for item in entry['result']['data'].get('items', [])) for entry in calls)
        checks['observed_marker'] = set(re.findall(r'MARKER_[a-f0-9]{16}', result.get('answer', ''))) == {marker}
        checks['original_preserved'] = Document(path).paragraphs[-1].text == marker
        report['outcome'] = result
        report['expected_marker'] = marker
        report['passed'] = all(checks.values())
    except BaseException as exc:
        runtime.logger.error('live_document_pages_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        report['finished'] = time.time()
        report_path = root / 'report.json'
        report_path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        runtime.close()
        print(json.dumps({'report': str(report_path), 'passed': report['passed'], 'checks': report['checks'],
                          'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
