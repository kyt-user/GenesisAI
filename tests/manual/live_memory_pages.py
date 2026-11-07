"""Opt-in Qwen Token Plan acceptance for long originals and dynamic memory pages."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import secrets
import time

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
    parser.add_argument('--execute', required=True, action='store_true')
    parser.add_argument('--model', default='qwen3.8-flash')
    args = parser.parse_args()
    load_environment()
    saved = load_settings()
    if saved is None or saved.provider != 'qwen_token_plan':
        saved = ProviderSettings(provider='qwen_token_plan', model=args.model)
    model, _ = build_model(settings=replace(saved, model=args.model, generation={'max_tokens': 2048},
        context={'max_input_tokens': 100000, 'history_tokens': 8000, 'strategy': 'basic'},
        request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240}))
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_memory_pages')
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    marker = 'TAIL_' + secrets.token_hex(8)
    original = '这是用于分页读取验收的完整原文。\n' + '记录：编号、内容、结果。\n' * 16000 + '\n验证标记：' + marker
    store.record.messages = [{'role': 'user', 'content': original},
                             {'role': 'assistant', 'content': '测试资料已存档，等待按引用读取。'}]
    store.commit()
    ref = store.history_projection()[0]['metadata']['history_ref']
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace), providers=[])
    report = {'provider': model.provider, 'model': model.model, 'session_id': store.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    def progress(kind, payload):
        if kind == 'model':
            print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result':
            print('tool_result', payload.get('ok'), (payload.get('error') or {}).get('code'), flush=True)
    try:
        runner = Runner(model, runtime, seconds=600, max_rounds=10, max_tools=12,
                        token_budget=100000, on_event=progress)
        result = runner.start('这是长原文分页验收。原文引用是 ' + ref +
            '。请先实际调用 memory_read(ref=该引用, view="content", offset=0)，首个调用不要传 limit，'
            '让应用动态分配页大小。读取返回的 total_chars 和 next_offset，然后再调用 memory_read 读取原文最后80个字符。'
            '必须实际读取两页，不能以摘要或预览代替。完成后只回答末尾的 TAIL_ 验证标记，不要联网或写文件。')
        entries = [entry for entry in store.record.calls.values()
                   if (entry.get('call') or {}).get('name') == 'memory_read']
        pages = [{'arguments': json.loads(entry['call']['arguments']), 'ok': entry.get('result', {}).get('ok'),
                  'data': entry.get('result', {}).get('data') or {}} for entry in entries]
        first = pages[0] if pages else {'arguments': {}, 'data': {}}
        checks = report['checks']
        checks['completed'] = result['status'] == 'completed'
        checks['actual_two_pages'] = len(pages) >= 2 and all(p['ok'] for p in pages)
        checks['default_dynamic_page'] = first['arguments'].get('offset', 0) == 0 and 'limit' not in first['arguments']
        checks['larger_than_old_default'] = len(first['data'].get('text', '')) > 6000
        checks['exact_cursor_and_length'] = (first['data'].get('next_offset') == len(first['data'].get('text', ''))
                                            and first['data'].get('total_chars') == len(original))
        checks['actual_tail_read'] = any(marker in p['data'].get('text', '') and p['data'].get('next_offset') is None for p in pages)
        checks['observed_random_marker'] = marker in result.get('answer', '')
        checks['original_unchanged_after_restore'] = Session(store.root, store.id).record.messages[0]['content'] == original
        tool_messages = [json.loads(m['content']) for m in store.record.messages if m['role'] == 'tool']
        checks['no_recursive_output_preview'] = all(not m.get('truncated') for m in tool_messages)
        report['pages'] = [{'arguments': p['arguments'], 'ok': p['ok'], 'chars': len(p['data'].get('text', '')),
                            'offset': p['data'].get('offset'), 'next_offset': p['data'].get('next_offset')} for p in pages]
        report['outcome'] = result
        report['expected_marker'] = marker
        report['passed'] = all(checks.values())
    except BaseException as exc:
        runtime.logger.error('live_memory_pages_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        report['finished'] = time.time()
        path = root / 'report.json'
        path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        runtime.close()
        print(json.dumps({'report': str(path), 'passed': report['passed'], 'checks': report['checks'],
                          'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
