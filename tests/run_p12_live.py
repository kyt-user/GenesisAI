"""Opt-in real CLI acceptance; uses public questions and isolated session data.

Run from project root: .venv/Scripts/python.exe -X utf8 tests/run_p12_live.py
No model/provider responses are mocked. Only terminal input/output are automated.
"""
import json
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
import genesisai.app.cli as cli


def run_record(data):
    """Return one self-contained audit row for the currently active run."""
    runtime = data.get('run_runtime') or {}
    return {
        'run_id': data.get('run_id'),
        'status': data.get('status'),
        'answer': data.get('answer', ''),
        'evidence_refs': list(runtime.get('evidence_refs', [])),
        'rounds': data.get('rounds', 0),
        'tool_count': data.get('tool_count', 0),
        'usage': json.loads(json.dumps(data.get('usage', {}))),
        'runtime_usage': json.loads(json.dumps(runtime.get('usage', {}))),
        'stop_reason': runtime.get('stop_reason'),
        'tool_activity': json.loads(json.dumps(runtime.get('tool_activity', []))),
        'context_report': json.loads(json.dumps(runtime.get('context_report', {}))),
    }


def main():
    parser = argparse.ArgumentParser(description='真实模型 CLI 验收：仅在允许向模型和公开网络发送测试查询后运行')
    parser.add_argument('--scope', choices=['session', 'run'], default='session')
    parser.add_argument('--scenario', choices=['apple', 'arduino', 'raspberry', 'nonproduct'], default='apple')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = root / 'docs' / 'temp' / 'P1.2_验收记录' / ('live-' + stamp + '-' + args.scope + '-' + args.scenario)
    output.mkdir(parents=True)
    os.environ['GENESISAI_HOME'] = str(output / 'state')
    original_build = cli.build_cli_session
    state = None
    actions = []
    question_index = 0
    scenarios = {
        'apple': [
            '查询 Apple 中国官网目前列出的一款 iPhone 产品，只介绍名称和规格，暂不查询售价。请读取官网正文并引用来源。',
            '它在中国大陆的起售价是多少？',
            '再说一下刚才查到的起售价。',
        ],
        'raspberry': [
            '读取 https://www.raspberrypi.com/products/raspberry-pi-5/ ，只介绍 Raspberry Pi 5 的名称和内存规格，暂不回答价格，并引用正文来源。',
            '该官方页面列出的最低美元价格是多少？',
            '再说一下刚才查到的最低美元价格。',
        ],
        'arduino': [
            '读取 https://store.arduino.cc/products/arduino-uno-rev3 ，只介绍 Arduino Uno Rev3 的名称和两项主要规格，暂不回答价格，并引用正文来源。',
            '该官方页面列出的欧元价格是多少？',
            '再说一下刚才查到的欧元价格。',
        ],
        'nonproduct': [
            '读取 https://www.rfc-editor.org/rfc/rfc9110.txt ，只回答该 RFC 的编号、标题和发布日期，并引用正文来源。',
            '它取代了哪些早期 RFC？',
            '再说一下刚才查到的标准标题。',
        ],
    }
    prompts = scenarios[args.scenario]

    def capture(*args, **kwargs):
        nonlocal state
        state = original_build(*args, **kwargs)
        return state

    def prompt(_view):
        nonlocal question_index
        if state.store.data['status'] == 'awaiting_confirmation':
            command = '/allow network run' if args.scope == 'run' else '/allow network'
            pending = state.store.data['pending'][0]
            if state.runtime.registry.get(pending['name']).spec.permission != 'network':
                command = '/reject'
        elif question_index < len(prompts):
            command = prompts[question_index]
            question_index += 1
        else:
            command = '/exit'
        actions.append(command)
        print('CLI input: ' + command, flush=True)
        transcript.write('\n❯ ' + command + '\n')
        transcript.flush()
        return command

    with (output / 'cli.txt').open('w', encoding='utf-8') as transcript:
        cli.Console = lambda **kwargs: Console(file=transcript, width=120, force_terminal=False)
        cli.build_cli_session = capture
        cli.CliView.prompt = prompt
        code = cli.main(['--workspace', str(output / 'workspace'), '--env-file', str(root.parents[1] / '.env'),
                         '--allow-remote-data', '--seconds', '180'])
    report = {'time_utc': stamp, 'exit_code': code, 'actions': actions, 'mode': 'real model and network', 'scope': args.scope,
              'scenario': args.scenario}
    if state:
        runs = list(state.store.data['history']) + [run_record(state.store.data)]
        report.update(session_id=state.store.id, model=state.snapshot.model_name,
                      history=state.store.data['history'], runs=runs, last_status=state.store.data['status'],
                      last_answer=state.store.data['answer'], last_runtime=state.store.data['run_runtime'])
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Live report: ' + str(output), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())

