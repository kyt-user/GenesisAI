"""Budgeted project references; selection never replaces the complete library."""

import json
import re

from genesisai.core.prompt.compaction import estimate_tokens
from genesisai.core.prompt.providers import ContextBlock
from genesisai.shared.messages import content_text


def pages(fetch):
    offset = 0
    while True:
        values = fetch(offset, 100)
        yield from values
        if len(values) < 100:
            return
        offset += len(values)


def interleave(*iterators):
    pending = list(map(iter, iterators))
    while pending:
        for iterator in list(pending):
            try:
                yield next(iterator)
            except StopIteration:
                pending.remove(iterator)


def references(runtime, task):
    budget = runtime.context_budgeter.config.project_context_tokens
    latest = next((content_text(item.get('content')) for item in reversed(runtime.store.record.messages)
                   if item['role'] == 'user'), '')
    text = latest + '\n' + (task or {}).get('objective', '')
    terms = list(dict.fromkeys(re.findall(r'[A-Za-z0-9_./\\-]+|[\u3400-\u9fff]+', text)))
    # Query selection is a relevance hint, not a truncation of the durable goal.
    # Bound SQL query complexity as well as prompt size; the full goal is in plan.
    selected = []
    for term in terms:
        if len(selected) >= 64 or estimate_tokens(' '.join([*selected, term])) > max(1, budget // 8):
            continue
        selected.append(term)
    query = ' '.join(selected)
    header = {
        'query': query, 'query_terms_selected': len(selected), 'query_terms_total': len(terms),
        'complete_catalog': False,
        'read_more': {'search': [{'tool': 'memory_search', 'query': query, 'corpus': corpus, 'offset': 0}
                                 for corpus in ('project', 'code')],
                      'read': {'tool': 'memory_read', 'ref': 'exact reference', 'offset': 0},
                      'files': {'tool': 'list_files', 'path': '.', 'offset': 0}},
    }
    intro = ('项目参考资料按当前请求相关度选取，未验证知识需核查，代码快照不能替代当前文件。'
             '目录受预算限制，未列出不代表不存在；memory_search 可分页搜索完整库，list_files 可分页浏览文件。\n'
             + json.dumps(header, ensure_ascii=False))
    yield ContextBlock('project_index', intro, scope='workspace', token_budget=budget)
    remaining = budget - estimate_tokens(intro)
    relevant = (interleave(*(pages(lambda offset, limit, corpus=corpus: runtime.memory.search(
        query, corpus=corpus, offset=offset, limit=limit)) for corpus in ('project', 'code'))) if query else iter(()))
    fallback = interleave(
        (dict(row, ref=row['id']) for row in pages(lambda offset, limit: runtime.memory.notes(offset=offset, limit=limit))),
        (dict(kind='code', title=row['path'], sha256=row['sha256'], ref=row['ref'],
              symbol_count=len(row['symbols']), coverage=row['coverage'])
         for row in pages(lambda offset, limit: runtime.project_index.overview(offset=offset, limit=limit))),
    )
    seen = set()
    for stream in (relevant, fallback):
        for item in stream:
            if item['ref'] in seen or item.get('status') not in {None, 'active', 'unverified'}:
                continue
            seen.add(item['ref'])
            body = json.dumps(item, ensure_ascii=False)
            cost = estimate_tokens(body)
            if cost > remaining:
                # Keep the exact reference when the excerpt will not fit.
                body = json.dumps({key: item[key] for key in ('ref', 'title', 'kind', 'status') if key in item}
                                  | {'excerpt_omitted': True}, ensure_ascii=False)
                cost = estimate_tokens(body)
            if cost > remaining:
                return
            yield ContextBlock('project_reference:' + item['ref'], body, scope='workspace', token_budget=cost)
            remaining -= cost
