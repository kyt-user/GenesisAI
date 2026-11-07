"""Budgeted user-input views backed by exact, durable originals."""

import copy
import json

from genesisai.core.prompt.compaction import estimate_tokens
from genesisai.shared.messages import content_text, image_parts


def preview(text, budget, read_more):
    def render(count):
        head = (count * 3) // 4
        tail = count - head
        return {'notice': '完整原文已保存；这里只是分页预览。未读部分不能当作已审阅；先按引用读取相关完整指令和资料再行动。',
                'total_chars': len(text), 'omitted_range': [head, len(text) - tail],
                'read_more': {**read_more, 'offset': head},
                'prefix': text[:head], 'suffix': text[-tail:] if tail else ''}
    # This object becomes message text; account for the outer wire JSON escaping.
    cost = lambda count: estimate_tokens(json.dumps(render(count), ensure_ascii=False))
    if cost(0) > budget:
        raise ValueError('Context budget cannot fit the input reference and reading instructions')
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if cost(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    return render(low)


def project(messages, budget):
    values, references = copy.deepcopy(messages), []
    for message in values:
        if message['role'] != 'user':
            continue
        text = content_text(message.get('content'))
        if estimate_tokens(text) <= budget:
            continue
        ref = (message.get('metadata') or {}).get('history_ref')
        if not ref:
            raise ValueError('Oversized user input requires a persisted history reference')
        view = preview(text, budget, {'tool': 'memory_read', 'ref': ref, 'view': 'content'})
        projected = json.dumps(view, ensure_ascii=False)
        images = image_parts(message.get('content'))
        message['content'] = [{'type': 'text', 'text': projected}, *images] if images else projected
        message.setdefault('metadata', {})['input_projection'] = {'ref': ref, 'total_chars': len(text)}
        references.append({'ref': ref, 'total_chars': len(text), 'omitted_range': view['omitted_range']})
    return values, references


def restore(messages, original):
    sources = {(item.get('metadata') or {}).get('history_ref'): item for item in original if item['role'] == 'user'}
    values = copy.deepcopy(messages)
    for item in values:
        marker = (item.get('metadata') or {}).get('input_projection')
        if marker and marker['ref'] in sources:
            item['content'] = copy.deepcopy(sources[marker['ref']]['content'])
            item['metadata'].pop('input_projection')
    return values
