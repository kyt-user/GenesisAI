"""Revalidate calculation provenance without promoting model-written formulas."""
import json
import hashlib
from genesisai.shared.ids import sha
from genesisai.shared.messages import content_text
from genesisai.shared.prices import calculate, price_claims
from genesisai.shared.security import ToolError, digest


def source_document(store, ref):
    item = store.record.sources.get(ref)
    if not isinstance(item, dict) or item.get('stale'):
        raise ValueError('来源未登记或已经过期')
    if item.get('kind') in {'web', 'web_render', 'private_web'}:
        page = json.loads((store.root / 'sources' / (ref + '.json')).read_text(encoding='utf-8'))
        if digest(page['text']) != item.get('sha256'): raise ValueError('来源正文哈希不匹配')
        return item, page['text']
    if item.get('kind') == 'price_calculation':
        return item, item['text']
    if item.get('kind') == 'file': return item, None
    raise ValueError('此来源类型不能作为价格操作数')


def valid_calculation(store, access, ref):
    states, pending = {}, [(ref, False)]
    try:
        while pending:
            identity, complete = pending.pop()
            source = store.record.sources.get(identity)
            data = source['calculation']
            if complete:
                assumed = any(item['proof']['kind'] == 'user' or
                    (item['proof']['kind'] == 'source' and store.record.sources.get(item['proof']['ref'], {}).get('user_assumptions', False))
                    for item in data['operands'])
                if source.get('user_assumptions') is not assumed: return False
                states[identity] = 2
                continue
            if states.get(identity) == 2: continue
            if states.get(identity) == 1: return False
            encoded = json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
            if identity != 'src_' + hashlib.sha256(encoded).hexdigest()[:16]: return False
            if source.get('stale') or source['owner_session_id'] != store.id or source['workspace_id'] != store.record.workspace_id:
                return False
            if digest(data) != source['sha256'] or calculate(data['operation'], data['operands'], data['decimal_places']) != data['result']:
                return False
            if source['text'] != f"{data['result']['currency']} {data['result']['amount']}": return False
            states[identity] = 1
            pending.append((identity, True))
            for operand in data['operands']:
                proof = operand['proof']
                if proof['kind'] == 'user':
                    message = store.record.messages[proof['message_index']]
                    body = content_text(message.get('content'))
                    if message.get('role') != 'user' or digest(body) != proof['sha256'] or operand['quote'] not in body:
                        return False
                elif proof['kind'] == 'source':
                    item, body = source_document(store, proof['ref'])
                    if digest(item) != proof['metadata_hash']: return False
                    if item['kind'] == 'file':
                        path = access.read(item['path'])
                        if sha(path) != item['sha256']: return False
                    elif item['kind'] == 'price_calculation':
                        pending.append((proof['ref'], False))
                        if operand['quote'] not in body: return False
                    elif operand['quote'] not in body: return False
                else: return False
        return True
    except (KeyError, IndexError, ValueError, TypeError, OSError, ToolError, ArithmeticError):
        # A stale, missing, unauthorized or malformed dependency cannot support
        # a price. Runtime failures don't turn unverified amounts into evidence.
        return False


def calculation_prices(store, access, ref):
    if not valid_calculation(store, access, ref): return set()
    prices, seen, pending = set(), set(), [ref]
    while pending:
        identity = pending.pop()
        if identity in seen: continue
        seen.add(identity)
        source = store.record.sources.get(identity)
        result = source['calculation']['result']
        prices.add((result['currency'], result['amount']))
        for item in source['calculation']['operands']:
            prices.update(price_claims(item['quote']))
            proof = item['proof']
            if proof['kind'] == 'source' and store.record.sources.get(proof['ref'], {}).get('kind') == 'price_calculation':
                pending.append(proof['ref'])
    return prices
