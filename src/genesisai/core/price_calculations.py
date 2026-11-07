"""Bind every Decimal operand to scoped source text or an explicit user quote."""
from genesisai.shared.filetypes import detect_file_type
from genesisai.shared.ids import sha
from genesisai.shared.messages import content_text
from genesisai.shared.price_evidence import source_document, valid_calculation
from genesisai.shared.prices import calculate
from genesisai.shared.security import ToolError, digest


def file_contains(context, source, quote):
    from genesisai.core.extensions.tools.filesystem import text_ranges
    from genesisai.core.extensions.tools.office.readers import iter_text
    path = context.access.read(source['path'])
    if sha(path) != source['sha256']: raise ToolError('stale_source', '文件来源已变化，请重新读取')
    kind = detect_file_type(path)
    def chunks():
        if kind in {'docx', 'xlsx', 'pptx', 'pdf'}:
            yield from iter_text(path, kind)
        else:
            info = text_ranges.metadata(path)
            with path.open(encoding=info['encoding'], newline='') as stream:
                while value := stream.read(text_ranges.CHUNK): yield value
    window, found = '', False
    for chunk in chunks():
        window = window[-len(quote):] + chunk
        if quote in window:
            found = True
            break
    if sha(path) != source['sha256']: raise ToolError('stale_source', '引用验证期间文件发生变化')
    return found


def calculate_price(context, args):
    operands, user_assumptions = [], False
    for supplied in args['operands']:
        item = dict(supplied)
        quote = item['quote']
        ref = item.pop('source_ref')
        if ref == 'user':
            messages = list(context.store.record.messages)
            index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get('role') == 'user'), None)
            body = content_text(messages[index].get('content')) if index is not None else ''
            if not quote or quote not in body:
                raise ToolError('unsupported_operand', 'user操作数必须逐字引用当前用户原消息，不接受模型补写的数字')
            item['proof'] = {'kind': 'user', 'message_index': index, 'sha256': digest(body)}
            user_assumptions = True
        else:
            try: source, body = source_document(context.store, ref)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise ToolError('unsupported_operand', '来源未在当前会话登记、已过期或正文损坏') from exc
            if source['kind'] == 'price_calculation':
                if not valid_calculation(context.store, context.access, ref):
                    raise ToolError('unsupported_operand', '派生来源的计算或依赖已经失效')
                user_assumptions |= source.get('user_assumptions', False)
            found = file_contains(context, source, quote) if source['kind'] == 'file' else quote in body
            if not quote or not found:
                raise ToolError('unsupported_operand', 'quote必须是已读来源中逐字存在的原文')
            item['proof'] = {'kind': 'source', 'ref': ref, 'metadata_hash': digest(source)}
        operands.append(item)
    places = args.get('decimal_places', 2)
    result = calculate(args['operation'], operands, places)
    calculation = {'operation': args['operation'], 'operands': operands, 'decimal_places': places, 'result': result}
    text = result['currency'] + ' ' + result['amount']
    ref = context.store.source(kind='price_calculation', text=text, calculation=calculation,
        sha256=digest(calculation), owner_session_id=context.store.id, workspace_id=context.store.record.workspace_id,
        user_assumptions=user_assumptions, title='有来源的价格计算')
    state = context.store.record.run_runtime
    if ref not in state['evidence_refs']: state['evidence_refs'].append(ref)
    context.store.commit()
    public_operands = [{**{key: value for key, value in item.items() if key != 'proof'},
                       'source_ref': item['proof'].get('ref', 'user')} for item in operands]
    return {'source_refs': [ref], 'source_ref': ref, 'source_text': text, **result, 'operands': public_operands,
            'basis': 'user_assumptions' if user_assumptions else 'cited_sources',
            'statement': '基于用户给定前提的计算，不是核实的市场报价' if user_assumptions else '基于所引来源的派生计算，不代表当前市场报价'}
