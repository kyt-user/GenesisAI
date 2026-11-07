"""Decimal price parsing and deterministic calculations; no inferred arithmetic."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from genesisai.shared.security import ToolError

CURRENCIES = {'cny': 'CNY', 'rmb': 'CNY', '人民币': 'CNY', '元': 'CNY', '¥': 'CNY', '￥': 'CNY',
    'usd': 'USD', '美元': 'USD', '$': 'USD', 'eur': 'EUR', '欧元': 'EUR', '€': 'EUR',
    'gbp': 'GBP', '英镑': 'GBP', '£': 'GBP', 'jpy': 'JPY', '日元': 'JPY',
    'hkd': 'HKD', '港元': 'HKD', 'aud': 'AUD', 'cad': 'CAD'}
NUMBER = r'[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?'
CODE = r'CNY|RMB|USD|EUR|GBP|JPY|HKD|AUD|CAD'
PREFIX = rf'(?<![A-Za-z0-9_])(?:{CODE})|人民币|美元|欧元|英镑|日元|港元|[¥￥$€£]'
SUFFIX = rf'(?:{CODE})(?![A-Za-z0-9_])|人民币|美元|欧元|英镑|日元|港元|元'
PRICE_CLAIM = re.compile(rf'(?P<sign>[+-]?)(?P<currency>{PREFIX})\s*'
    rf'(?P<amount>{NUMBER})(?![A-Za-z0-9_]|[.,][0-9])|'
    rf'(?<![A-Za-z0-9_,.])(?P<suffix_amount>{NUMBER})\s*(?P<suffix_currency>{SUFFIX})', re.I)


def decimal(value):
    if not isinstance(value, str) or len(value) > 120 or not re.fullmatch(NUMBER, value):
        raise ToolError('invalid_amount', '金额/数量使用十进制字符串，可含正确千分位，不接受指数、NaN或无穷值')
    try: result = Decimal(value.replace(',', ''))
    except InvalidOperation: raise ToolError('invalid_amount', '无效十进制金额') from None
    if len(result.as_tuple().digits) > 80 or abs(result.as_tuple().exponent) > 24:
        raise ToolError('invalid_amount', '数值超过80位或24位小数的计算精度范围')
    return result


def canonical(value):
    value = value if isinstance(value, Decimal) else decimal(value)
    if not value: return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def price_claims(content):
    values = set()
    for match in PRICE_CLAIM.finditer(content or ''):
        code = match['currency'] or match['suffix_currency']
        raw = match['amount'] or match['suffix_amount']
        if match['sign']:
            if raw[0] in '+-': continue
            raw = match['sign'] + raw
        try: values.add((CURRENCIES[code.casefold()], canonical(raw)))
        except ToolError: continue
    return values


def currency(value):
    if value not in set(CURRENCIES.values()):
        raise ToolError('invalid_currency', '使用明确币种代码：' + ', '.join(sorted(set(CURRENCIES.values()))))
    return value


def verify_operand(item):
    required = {'money': {'currency'}, 'quantity': set(), 'rate': {'from_currency', 'to_currency'}}.get(item.get('type'))
    if required is None or not required.issubset(item):
        raise ToolError('validation_error', '金额操作数需要currency；汇率操作数需要from_currency及to_currency')
    value = decimal(item['value'])
    quote, kind = item['quote'], item['type']
    if kind == 'money':
        if (currency(item['currency']), canonical(value)) not in price_claims(quote):
            raise ToolError('unsupported_operand', '引用原文未包含该币种和金额')
    elif kind == 'quantity':
        matches = re.findall(rf'(?:数量|qty|quantity)\s*[:=：]?\s*({NUMBER})(?![A-Za-z0-9_,.])|'
                             rf'(?<![A-Za-z0-9_,.])({NUMBER})\s*(?:件|个|台|套|份|人|小时|分钟|次|天|月|年|units?\b|items?\b|tokens?\b|hours?\b)', quote, re.I)
        if value < 0 or not any(canonical(a or b) == canonical(value) for a, b in matches):
            raise ToolError('unsupported_operand', '数量必须在原文中明确标注数量或单位，且不得为负数')
    elif kind == 'rate':
        base, target = currency(item['from_currency']), currency(item['to_currency'])
        codes = lambda c: '(?:' + '|'.join(re.escape(k) for k, v in CURRENCIES.items() if v == c) + ')'
        pattern = rf'(?<![A-Za-z0-9_])1(?:\.0+)?\s*{codes(base)}\s*(?:=|兑换|兑|equals)\s*({NUMBER})\s*{codes(target)}(?![A-Za-z0-9_])'
        if value <= 0 or base == target or not any(canonical(raw) == canonical(value) for raw in re.findall(pattern, quote, re.I)):
            raise ToolError('unsupported_operand', '汇率原文须明确方向，例如 1 USD = 7.2 CNY；汇率必须为正数')
    else:
        raise ToolError('validation_error', '操作数类型无效')
    return value


def calculate(operation, operands, places=2):
    if type(places) is not int or not 0 <= places <= 8:
        raise ToolError('validation_error', '小数位必须为0至8')
    if not operands or len(operands) > 100:
        raise ToolError('validation_error', '每次计算需1至100个有证据操作数')
    values = [verify_operand(item) for item in operands]
    with localcontext() as ctx:
        ctx.prec = 180
        if operation == 'sum':
            if any(item['type'] != 'money' for item in operands) or len({item['currency'] for item in operands}) != 1:
                raise ToolError('currency_mismatch', '费用合计只接受相同币种金额；跨币种请先使用有来源汇率换算')
            code, total = operands[0]['currency'], sum(values, Decimal(0))
            formula = ' + '.join('(' + canonical(v) + ')' for v in values)
        elif operation == 'multiply':
            if len(operands) != 2 or [item['type'] for item in operands] != ['money', 'quantity']:
                raise ToolError('validation_error', '数量乘单价依次提供money与quantity操作数')
            code, total = operands[0]['currency'], values[0] * values[1]
            formula = f'{canonical(values[0])} * {canonical(values[1])}'
        elif operation == 'convert':
            if len(operands) != 2 or [item['type'] for item in operands] != ['money', 'rate'] or operands[0]['currency'] != operands[1]['from_currency']:
                raise ToolError('currency_mismatch', '换算依次提供金额和方向匹配的汇率')
            code, total = operands[1]['to_currency'], values[0] * values[1]
            formula = f'{canonical(values[0])} {operands[0]["currency"]} * {canonical(values[1])} {code}/{operands[1]["from_currency"]}'
        else: raise ToolError('validation_error', '不支持该计算操作')
        rounded = total.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return {'currency': code, 'amount': canonical(rounded), 'unrounded': canonical(total),
                'formula': formula, 'decimal_places': places, 'rounding': 'ROUND_HALF_UP'}
