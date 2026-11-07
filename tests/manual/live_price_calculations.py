"""Real Qwen Token Plan acceptance for source-bound Decimal price calculations."""
import argparse
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
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
from genesisai.shared.price_evidence import valid_calculation
from genesisai.shared.prices import canonical, price_claims
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
    model, _ = build_model(settings=replace(saved, model=args.model, generation={'max_tokens': 3072},
        context={'max_input_tokens': 100000, 'strategy': 'basic'},
        request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240}))
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_price')
    workspace = root / 'workspace'; workspace.mkdir(parents=True)
    unit = Decimal(secrets.randbelow(9900) + 100) / 100
    quantity = secrets.randbelow(8) + 2
    fee = Decimal(secrets.randbelow(450) + 50) / 100
    rate = Decimal('7.23')
    expected = canonical(((unit * quantity + fee) * rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    session = Session(root / 'state', workspace=workspace)
    session.record.apply(grants=[str(workspace)], output=str(workspace)); session.commit()
    runtime = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), providers=[])
    report = {'provider': model.provider, 'model': model.model, 'session_id': session.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    def progress(kind, payload):
        if kind == 'model': print('model_round', payload.get('round'), flush=True)
        elif kind == 'tool_result': print('tool_result', payload.get('ok'), flush=True)
    try:
        prompt = (f'我明确给定计算前提：单价 USD {unit:.2f}，数量{quantity}件，运费 USD {fee:.2f}，汇率 1 USD = {rate} CNY。'
                  '请实际用 calculate_price 依次计算数量乘单价、加运费合计，再换算成人民币，保留两位小数。'
                  '先 discover_tools 找工具；每项操作数须绑定我上述原话或前一步返回来源。'
                  '最终引用最后一个派生来源，明确标注基于用户给定前提，不把它称为实时市场报价。不要联网、修改文件或猜测工具结果。')
        outcome = Runner(model, runtime, seconds=600, max_rounds=12, max_tools=16,
                         token_budget=60000, on_event=progress).start(prompt)
        calls = [entry for entry in session.record.calls.values()
                 if (entry.get('call') or {}).get('name') == 'calculate_price' and entry.get('result', {}).get('ok')]
        operations = [json.loads(entry['call']['arguments'])['operation'] for entry in calls]
        results = [entry['result']['data'] for entry in calls]
        final = [item for item in results if item.get('currency') == 'CNY' and item.get('amount') == expected]
        answer = outcome.get('answer', '')
        report['checks'] = {'completed': outcome['status'] == 'completed',
            'three_actual_operations': all(name in operations for name in ('multiply', 'sum', 'convert')),
            'exact_tool_result': bool(final),
            'bound_sources_valid': bool(final) and all(valid_calculation(session, runtime.access, item['source_ref']) for item in results),
            'final_reference': bool(final) and final[-1]['source_ref'] in answer,
            'answer_amount': ('CNY', expected) in price_claims(answer),
            'user_basis': '用户' in answer and any(word in answer for word in ('给定', '提供', '假设'))}
        report.update(outcome=outcome, expected_cny=expected, passed=all(report['checks'].values()))
    except BaseException as exc:
        runtime.logger.error('live_price_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        report['finished'] = time.time()
        path = root / 'report.json'
        path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        runtime.close()
        print(json.dumps({'report': str(path), 'passed': report['passed'], 'checks': report['checks'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
