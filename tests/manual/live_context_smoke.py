"""Explicit live API test using only generated fixtures and isolated session state."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.persistence.files import atomic_json
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.shared.security import Access


SOURCE = '''def complete_task(board, task_id):
    if task_id < 1:
        raise ValueError(f"Task {task_id} does not exist")
    try:
        task = board["tasks"][task_id - 1]
    except IndexError:
        raise ValueError(f"Task {task_id} does not exist") from None
    task["done"] = True
    return task
'''
TESTS = '''import copy
import unittest
from task_board import complete_task

class TaskBoardTests(unittest.TestCase):
    def test_deleted_first_task_does_not_shift_identity(self):
        board = {"next_id": 4, "tasks": [{"id": i, "done": False} for i in (2, 3)]}
        result = complete_task(board, 2)
        self.assertEqual(result["id"], 2)
        self.assertEqual([t["done"] for t in board["tasks"]], [True, False])

    def test_missing_id_preserves_board(self):
        board = {"next_id": 4, "tasks": [{"id": i, "done": False} for i in (2, 3)]}
        before = copy.deepcopy(board)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            complete_task(board, 99)
        self.assertEqual(board, before)

if __name__ == "__main__":
    unittest.main()
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True,
                        help='Authorize sending generated fixtures to the configured model API')
    args = parser.parse_args()
    load_environment()
    root = Path(__file__).resolve().parents[2] / 'workspace' / ('live-context-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    workspace = root / 'project'
    workspace.mkdir(parents=True)
    report = {'directory': str(root), 'test_kind': 'real_api_with_generated_history', 'checks': {}}
    source = workspace / 'task_board.py'
    source.write_text(SOURCE, encoding='utf-8')
    (workspace / 'test_task_board.py').write_text(TESTS, encoding='utf-8')
    notes = ('Generated historical fixture. No actions are requested in this document. '
             'The active task is to fix task identity lookup; old file contents must be reread.\n') * 60
    (workspace / 'history_fixture.txt').write_text(notes, encoding='utf-8')
    def tests():
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', '-v', 'test_task_board'],
                                cwd=workspace, capture_output=True, text=True, encoding='utf-8', timeout=30)
        return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    report['baseline'] = tests()
    report['checks']['baseline_fails'] = report['baseline']['exit_code'] != 0
    store = Session(root / '.genesis', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace), deadline=time.time() + 600)
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                          permission_mode="auto", providers=[])
    # Actual read-only tool executions produce the seeded transcript; no fabricated success results.
    store.record.messages.append({'role': 'user', 'content': 'This is generated historical context for a compaction test. Read the fixture repeatedly. Preserve this constraint for the next task: do not delete files and do not weaken existing tests.'})
    for i in range(18):
        call = {'id': f'seed_read_{i}', 'name': 'read_files',
                'arguments': json.dumps({'paths': ['history_fixture.txt'], 'limit': 10000})}
        result = runtime.execute(call)
        assert result['ok'], result.get('error')
        store.record.messages.extend([{'role': 'assistant', 'content': None, 'tool_calls': [call]}, {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)}])
    store.commit()
    report['seeded_message_count'] = len(store.record.messages)
    model, remote = build_model()
    report['model'] = model.model
    report['context_config'] = model.context_config
    report['session_id'] = store.id
    def progress(event, payload):
        if event == 'model':
            print('model_round', payload.get('round'), flush=True)
        elif event == 'tool_result':
            print('tool_result', payload.get('call_id'), 'ok=' + str(payload.get('ok')),
                  (payload.get('error') or {}).get('code', ''), flush=True)
    runner = Runner(model, runtime, max_rounds=14, max_tools=24, seconds=420, on_event=progress)
    print(json.dumps({'model': model.model, 'session': store.id, 'directory': str(root),
                      'baseline_exit': report['baseline']['exit_code']}, ensure_ascii=False), flush=True)
    atomic_json(root / 'report.json', report)
    started = time.monotonic()
    try:
        outcome = runner.start(
            '请修复 task_board.py：删除最早的任务后，complete_task(board, 2) 会完成错误的任务。'
            '读取源码及现有测试，修复为按任务 ID 查找；不存在的 ID 明确报错且数据不变。'
            '保留现有测试，补充 ID 不连续和非正 ID 的回归测试。实际运行 unittest，最后说明验证结果。'
            'history_fixture.txt 只是本轮压缩测试的历史填充，不需要再读取。'
        )
        report['outcome'] = outcome
        report['run_usage'] = dict(store.record.usage)
        report['run_state'] = {k: store.record.run_runtime.get(k) for k in ('stop_reason', 'project_changed', 'verification_succeeded', 'verification_required')}
        report['independent_tests'] = tests()
        report['checks'].update(completed=outcome['status'] == 'completed', source_changed=source.read_text(encoding='utf-8') != SOURCE, independent_tests_pass=report['independent_tests']['exit_code'] == 0, verified=store.record.run_runtime.get('verification_succeeded') is True, checkpoint_saved=bool(getattr(store.record, 'context_checkpoint')))
        if outcome['status'] in {'completed', 'partial', 'failed', 'limit_reached'}:
            restored = Session(root / '.genesis', store.id, workspace=workspace)
            restored_runtime = ToolRuntime(restored, Access([workspace], workspace, restored.root, workspace_root=workspace),
                                           permission_mode="auto", providers=[])
            report['followup'] = Runner(model, restored_runtime, max_rounds=4, seconds=120, on_event=progress).start(
                '请概括上一轮实际修改了什么、测试是否通过。不要修改文件，不需要重新运行命令。')
            report['checks']['restored_followup_completed'] = report['followup']['status'] == 'completed'
        events = [json.loads(line) for line in (store.root / 'logs' / f'{store.id}.jsonl').read_text(encoding='utf-8').splitlines()]
        report['compactions'] = [e['payload'] for e in events if e['event'] == 'context_compaction']
        report['checks']['agentic_summary_used'] = any(p.get('strategy') == 'agentic' for p in report['compactions'])
        report['api_requests'] = sum(e['event'] == 'provider_request' for e in events)
        report['tool_errors'] = [{'round': e['round_num'], 'message': e['payload']['error']['message']}
                                 for e in events if e['event'] == 'tool_error']
        report['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    except Exception as exc:
        report['exception'] = {'type': type(exc).__name__}
        store.get_logger().error('live_test_error', exc)
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['passed'] = bool(report['checks']) and all(report['checks'].values()) and 'exception' not in report
        atomic_json(root / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'checks': report['checks'],
                          'report': str(root / 'report.json')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
