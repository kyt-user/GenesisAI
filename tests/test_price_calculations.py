"""Derived price evidence must be executable, Decimal-exact and source-bound."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from genesisai.agent.recovery import RecoveryMixin
from genesisai.core.persistence.session import Session
from genesisai.core.price_calculations import calculate_price
from genesisai.shared.ids import sha
from genesisai.shared.price_evidence import valid_calculation
from genesisai.shared.prices import decimal, price_claims
from genesisai.shared.security import Access, ToolError, digest


@pytest.fixture
def context(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    output = tmp_path / 'output'
    store.record.apply(grants=[str(workspace)], output=str(output))
    store.commit()
    return SimpleNamespace(store=store, access=Access([workspace], output, store.root, workspace_root=workspace))


def user(context, text):
    context.store.record.messages.append({'role': 'user', 'content': text})
    context.store.commit()


def money(value, quote, ref='user', code='USD'):
    return {'type': 'money', 'value': value, 'currency': code, 'source_ref': ref, 'quote': quote}


def web(context, text):
    ref = context.store.source(kind='web', url='https://example.test/prices', title='Price', sha256=digest(text))
    folder = context.store.root / 'sources'; folder.mkdir(exist_ok=True)
    (folder / (ref + '.json')).write_text(json.dumps({'text': text}), encoding='utf-8')
    context.store.record.run_runtime['evidence_refs'].append(ref)
    context.store.commit()
    return ref


def unsupported(context, text):
    checker = RecoveryMixin()
    checker.store, checker.runtime = context.store, SimpleNamespace(access=context.access)
    return checker._unsupported_price_claims(text)


def test_user_quantity_fee_total_and_formula_citation(context):
    user(context, '单价 USD 9.99，数量3件，运费 USD 1.50。请按这些给定值算费用。')
    multiplied = calculate_price(context, {'operation': 'multiply', 'operands': [money('9.99', 'USD 9.99'),
        {'type': 'quantity', 'value': '3', 'source_ref': 'user', 'quote': '数量3件'}]})
    assert multiplied['amount'] == '29.97'
    total = calculate_price(context, {'operation': 'sum', 'operands': [
        money('29.97', 'USD 29.97', multiplied['source_ref']), money('1.5', 'USD 1.50')]})
    assert total['amount'] == '31.47' and total['basis'] == 'user_assumptions'
    answer = f"基于用户给定前提：USD 9.99 * 3 + USD 1.50 = USD 31.47 [{total['source_ref']}]"
    assert not unsupported(context, answer)
    assert unsupported(context, f"当前市场报价 USD 31.47 [{total['source_ref']}]")
    assert unsupported(context, 'USD 9.99 * 3 + USD 1.50 = USD 31.47')


def test_source_exchange_direction_and_currency_are_bound(context):
    ref = web(context, 'Unit price: USD 12.50. Published rate: 1 USD = 7.2 CNY.')
    result = calculate_price(context, {'operation': 'convert', 'operands': [money('12.5', 'USD 12.50', ref),
        {'type': 'rate', 'value': '7.2', 'from_currency': 'USD', 'to_currency': 'CNY',
         'source_ref': ref, 'quote': '1 USD = 7.2 CNY'}]})
    assert result['amount'] == '90' and result['currency'] == 'CNY' and result['basis'] == 'cited_sources'
    assert not unsupported(context, f"按来源汇率换算 CNY 90 [{result['source_ref']}]")
    assert unsupported(context, f"JPY 90 [{result['source_ref']}]")
    with pytest.raises(ToolError, match='方向'):
        calculate_price(context, {'operation': 'convert', 'operands': [money('12.5', 'USD 12.50', ref),
            {'type': 'rate', 'value': '7.2', 'from_currency': 'CNY', 'to_currency': 'USD',
             'source_ref': ref, 'quote': '1 USD = 7.2 CNY'}]})


def test_source_replacement_staleness_and_reopening(context):
    ref = web(context, 'Service USD 10.01 and fee USD 0.02')
    result = calculate_price(context, {'operation': 'sum', 'operands': [money('10.01', 'USD 10.01', ref), money('0.02', 'USD 0.02', ref)]})
    assert result['amount'] == '10.03'
    reopened = Session(context.store.root, context.store.id)
    assert valid_calculation(reopened, context.access, result['source_ref'])
    (context.store.root / 'sources' / (ref + '.json')).write_text(json.dumps({'text': 'Service USD 99'}), encoding='utf-8')
    assert not valid_calculation(context.store, context.access, result['source_ref'])
    assert unsupported(context, f"USD 10.03 [{result['source_ref']}]")


def test_file_source_hash_and_current_authorization(context, tmp_path):
    path = context.access.workspace / 'fees.txt'; path.write_text('Fee USD 1.25', encoding='utf-8')
    ref = context.store.source(kind='file', path=str(path), sha256=sha(path), title=path.name)
    result = calculate_price(context, {'operation': 'sum', 'operands': [money('1.25', 'USD 1.25', ref)]})
    assert valid_calculation(context.store, context.access, result['source_ref'])
    other = tmp_path / 'other'; other.mkdir()
    restricted = Access([other], other / 'out', context.store.root, workspace_root=other)
    assert not valid_calculation(context.store, restricted, result['source_ref'])
    path.write_text('Fee USD 2.50', encoding='utf-8')
    assert not valid_calculation(context.store, context.access, result['source_ref'])


def test_user_hash_cross_session_and_tampered_result_are_rejected(context):
    user(context, '假设价格 USD 5.00。')
    result = calculate_price(context, {'operation': 'sum', 'operands': [money('5', 'USD 5.00')]})
    ref = result['source_ref']
    original = deepcopy(context.store.record.sources[ref])
    original_message = deepcopy(context.store.record.messages[-1])
    context.store.record.messages[-1]['content'] += '更新说明'
    assert not valid_calculation(context.store, context.access, ref)
    context.store.record.messages[-1] = original_message
    foreign = Session(context.store.root, workspace=context.access.workspace)
    foreign.record.sources[ref] = deepcopy(original)
    foreign.record.messages = deepcopy(context.store.record.messages)
    assert not valid_calculation(foreign, context.access, ref)
    source = context.store.record.sources[ref]
    source['calculation']['result']['amount'] = '9999'
    source['sha256'] = digest(source['calculation'])
    source['text'] = 'USD 9999'
    assert not valid_calculation(context.store, context.access, ref)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '1e99', '12,34', '9.9identifier'])
def test_invalid_decimal_forms_never_become_operands(value):
    with pytest.raises(ToolError): decimal(value)


def test_decimal_thousands_negative_rounding_and_large_precision(context):
    user(context, '退款 -USD1.005；账单 USD 9,007,199,254,740,993.01；手续费 USD 0.02。')
    negative = calculate_price(context, {'operation': 'sum', 'operands': [money('-1.005', '-USD1.005')]})
    assert negative['amount'] == '-1.01' and negative['rounding'] == 'ROUND_HALF_UP'
    total = calculate_price(context, {'operation': 'sum', 'operands': [money('9,007,199,254,740,993.01', 'USD 9,007,199,254,740,993.01'), money('0.02', 'USD 0.02')]})
    assert total['amount'] == '9007199254740993.03'
    assert price_claims('USD -1.005; -USD1.005; -1.005美元') == {('USD', '-1.005')}


def test_forged_quote_arbitrary_multiplier_and_cross_currency_sum_fail(context):
    user(context, 'USD 10；USD 20；EUR 10；编号3。')
    with pytest.raises(ToolError, match='当前用户'):
        calculate_price(context, {'operation': 'sum', 'operands': [money('999', 'USD 999')]})
    with pytest.raises(ToolError, match='数量'):
        calculate_price(context, {'operation': 'multiply', 'operands': [money('10', 'USD 10'),
            {'type': 'quantity', 'value': '3', 'source_ref': 'user', 'quote': '编号3'}]})
    with pytest.raises(ToolError, match='相同币种'):
        calculate_price(context, {'operation': 'sum', 'operands': [money('10', 'USD 10'), money('10', 'EUR 10', code='EUR')]})
    with pytest.raises(ToolError, match='币种和金额'):
        calculate_price(context, {'operation': 'sum', 'operands': [money('20', 'USD 10')]})
