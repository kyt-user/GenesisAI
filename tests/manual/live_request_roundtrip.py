"""Opt-in Qwen Token Plan transport smoke: streaming tool call + non-stream reply."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from genesisai.app.cli import load_environment
from genesisai.core.persistence.session import Session
from genesisai.core.state.logger import RunLogger
from genesisai.model.config import build_model
from genesisai.model.providers.catalog import get_provider
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.ids import uid
from genesisai.shared.messages import Message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', required=True, action='store_true')
    parser.add_argument('--model', default='qwen3.8-flash')
    args = parser.parse_args()
    load_environment()
    saved = load_settings()
    if saved is None or saved.provider != 'qwen_token_plan':
        saved = ProviderSettings(provider='qwen_token_plan', model=get_provider('qwen_token_plan').default_model)
    settings = replace(saved, model=args.model, generation={'max_tokens': 2048},
                       request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240})
    model, _ = build_model(settings=settings)
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_request')
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    session = Session(root / 'state', workspace=workspace)
    logger = RunLogger(session)
    model.bind_logging(logger)
    report = {'provider': model.provider, 'model': model.model, 'started': time.time(),
              'request_timeouts': model.request_timeouts, 'checks': {}, 'passed': False}
    messages = [Message('user', '请先调用 double_integer 工具，参数 n=9；收到结果后只回复整数结果。')]
    tools = [{'type': 'function', 'function': {'name': 'double_integer', 'description': '返回整数的两倍',
              'parameters': {'type': 'object', 'properties': {'n': {'type': 'integer'}},
                             'required': ['n'], 'additionalProperties': False}}}]
    try:
        reply = list(model.stream_chat(messages, tools=tools))[-1]
        calls = reply.tool_calls or []
        report['checks']['stream_tool_call'] = (reply.finish_reason == 'tool_calls' and len(calls) == 1
            and calls[0].name == 'double_integer' and json.loads(calls[0].arguments) == {'n': 9})
        if not report['checks']['stream_tool_call']:
            raise ValueError('流式调用未返回约定工具与参数')
        arguments = json.loads(calls[0].arguments)
        result = {'result': 2 * arguments['n']}
        messages.extend([Message('assistant', reply.content, tool_calls=calls, reasoning=reply.reasoning),
                         Message('tool', json.dumps(result), tool_call_id=calls[0].id)])
        answer = model.chat(messages, tools=[])
        report['checks']['non_stream_reply'] = (answer.finish_reason == 'stop' and not answer.tool_calls
                                                and (answer.content or '').strip() == '18')
        report['passed'] = all(report['checks'].values())
    except BaseException as exc:
        logger.error('transport_smoke_error', exc)
        report['error'] = logger.journal.clean(str(exc))
    finally:
        report['finished'] = time.time()
        path = root / 'report.json'
        path.write_text(json.dumps(logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(path), 'passed': report['passed'], 'checks': report['checks']}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
