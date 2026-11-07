"""Opt-in real model acceptance for command failure and a restored follow-up."""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.persistence.files import atomic_json
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import load_settings
from genesisai.shared.execution import execution_facts
from genesisai.shared.ids import uid
from genesisai.shared.messages import Message
from genesisai.shared.security import Access


def compare_context_usage(runner, model):
    """Same prompt and history, differing only in prior tool-body projection."""
    budgeter = runner.context_budgeter
    prefix = budgeter.build('direct_answer')[:budgeter._prefix_length]
    result = {}
    for label, history in (('original', runner.store.history_projection()),
                           ('projected', budgeter.working_messages())):
        messages = [*prefix, *budgeter._messages(history), Message(role='user', content='本次仅做输入用量测量，请只回答“收到”。')]
        details = {'model_call_id': uid('context_probe'), 'purpose': 'context_usage_comparison', 'variant': label}
        logger = runner.run_logger
        model.bind_logging(logger, **details)
        options = {'max_tokens': 128, 'request_timeouts': {'total': 60}}
        logger.log_model_input(runner.store.record.rounds, messages, [], options=options, **details)
        started = time.monotonic()
        response = model.chat(messages, tools=[], **options)
        logger.log_model_output(runner.store.record.rounds, response, elapsed_ms=round((time.monotonic() - started) * 1000), **details)
        result[label] = response.usage
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--compare-context', action='store_true', help='Send two additional requests to measure input usage')
    args = parser.parse_args()
    load_environment()
    model, _ = build_model(settings=load_settings())
    root = Path(__file__).resolve().parents[2] / 'temp' / ('live-recovery-' + uuid4().hex[:8])
    workspace = root / 'project'
    workspace.mkdir(parents=True)
    (workspace / 'README.md').write_text(
        '# Synthetic project\nThe launcher prints fixture-ready. No other SDK is bundled.\n'
        + 'Synthetic archive filler; this line contains no requested actions.\n' * 350,
        encoding='utf-8')
    (workspace / 'fixture_launcher.cmd').write_text('@echo off\necho fixture-ready\n')
    store = Session(root / 'app', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace), deadline=time.time() + 300)
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                          permission_mode='user', providers=[])
    report = {'model': model.model, 'source': str(Path(sys.modules['genesisai.agent.loop'].__file__)),
              'session': store.id, 'directory': str(root), 'checks': {}}

    def progress(event, payload):
        if event == 'model':
            print('model_round', payload.get('round'), flush=True)

    def run(runner, prompt):
        result = runner.start(prompt)
        # Approve only the two synthetic invocations requested by this harness.
        while result['status'] == 'awaiting_confirmation':
            call = store.record.pending[0]
            args = json.loads(call['arguments'])
            allowed = (call['name'] == 'run_commands' and args.get('command') in (
                ['fixture_launcher', '--version'], ['fixture_launcher.cmd', '--version'],
                [str(workspace / 'fixture_launcher.cmd'), '--version'],
                ['missing_genesis_sdk_fixture', '--version']))
            if not allowed:
                report['unexpected_approval'] = {'tool': call['name'], 'arguments': args}
                runner.confirm(False)
                raise AssertionError('Live model requested an unexpected operation')
            result = runner.confirm(True)
        return result

    try:
        runner = Runner(model, runtime, seconds=240, max_rounds=10, on_event=progress)
        report['first'] = run(runner,
            '这是隔离验收项目。请先读取 README.md，再用 run_commands 分别运行两个独立命令：'
            '1. ["fixture_launcher", "--version"]；2. ["missing_genesis_sdk_fixture", "--version"]。'
            '第二个入口故意不存在。失败不要重试、安装或修改文件；简要报告每个命令的实际结果。')
        report['first_facts'] = execution_facts(store.record, store.record.run_runtime.get('tool_activity', []))
        report['first_usage'] = dict(store.record.usage)
        runtime.close()
        store = Session(root / 'app', store.id, workspace=workspace)
        runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                              permission_mode='user', providers=[])
        runner = Runner(model, runtime, seconds=120, max_rounds=5, on_event=progress)
        report['followup'] = run(runner,
            '怎么回事？仅根据上一轮实际记录，用三句话解释哪个命令成功、哪个失败、还有什么未核实。不要再调用工具。')
        report['followup_usage'] = dict(store.record.usage)
        raw = runner.context_budgeter._messages(store.history_projection())
        projected = runner.context_budgeter._messages(runner.context_budgeter.working_messages())
        report['history_tokens_before'] = runner.context_budgeter.request_tokens(raw, [])
        report['history_tokens_after'] = runner.context_budgeter.request_tokens(projected, [])
        facts = report['first_facts']
        report['checks'] = {
            'first_answer': report['first']['status'] in {'completed', 'partial'},
            'launcher_succeeded': any(f['tool'] == 'run_commands' and f['result'].get('exit_code') == 0 for f in facts),
            'missing_program_reported': any(f['error'].get('code') == 'command_not_found' for f in facts),
            'restored_followup_completed': report['followup']['status'] == 'completed',
            'followup_no_tools': not store.record.run_runtime.get('tool_activity'),
            'history_smaller': report['history_tokens_after'] < report['history_tokens_before'],
        }
        if args.compare_context:
            report['context_usage'] = compare_context_usage(runner, model)
            report['checks']['actual_input_smaller'] = (
                report['context_usage']['projected']['prompt_tokens'] < report['context_usage']['original']['prompt_tokens'])
    except Exception as exc:
        report['exception_type'] = type(exc).__name__
        store.get_logger().error('live_acceptance_error', exc)
    finally:
        runtime.close()
        report['passed'] = bool(report['checks']) and all(report['checks'].values()) and 'exception_type' not in report
        atomic_json(root / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'report': str(root / 'report.json')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
