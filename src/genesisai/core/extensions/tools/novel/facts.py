"""Paged heuristic extraction with exact evidence and a persistent chapter fact table."""
import re
from pathlib import Path

from genesisai.core.extensions.tools.filesystem import text_ranges
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError

NAMES = re.compile(r'\b[A-Z][a-z]{2,40}\b|[\u4e00-\u9fff]{2,4}(?=[说问道看向])')
AGES = re.compile(r'(?P<entity>[A-Z][a-z]{2,40})\s+(?:is|was)\s+(?P<value>\d+)\s+years? old|(?P<cn>[\u4e00-\u9fff]{2,4}?)(?:今年)?(?P<age>\d+)岁', re.I)


def read_block(context, args):
    target = context.access.workspace_path(args['path'], must_exist=True)
    digest = sha(target)
    if args.get('expected_sha256') is not None and args['expected_sha256'] != digest:
        raise ToolError('document_changed', '章节已变化，请重新开始覆盖扫描')
    offset = args.get('offset', 0)
    limit = min(args.get('limit', 50000), max(1, context.runtime.context_budgeter.read_page_tokens() * 3), 50000)
    start = max(0, offset - 64)
    text, info, _ = text_ranges.characters(target, start, limit + offset - start + 64)
    end = min(offset + limit, info['total_chars'])
    if offset > info['total_chars']:
        raise ToolError('validation_error', 'offset 超出章节正文')
    if sha(target) != digest:
        raise ToolError('document_changed', '章节在读取期间发生变化')
    return target, digest, text, start, offset, end, info


def extract_block(context, args):
    target, digest, text, start, offset, end, info = read_block(context, args)
    facts = []
    for match in NAMES.finditer(text):
        position = start + match.start()
        if offset <= position < end:
            facts.append({'entity': match.group(), 'attribute': 'mention', 'value': match.group(),
                          'offset': position, 'end_offset': start + match.end()})
    for match in AGES.finditer(text):
        position = start + match.start()
        if offset <= position < end:
            facts.append({'entity': match.group('entity') or match.group('cn'), 'attribute': 'age',
                          'value': match.group('value') or match.group('age'), 'offset': position,
                          'end_offset': start + match.end(), 'evidence': match.group()})
    paragraphs = []
    body = text[offset-start:end-start]
    for match in re.finditer(r'[^\n]+', body):
        value = match.group()
        position = offset + match.start()
        paragraphs.append({'offset': position, 'length': len(value), 'text': value,
                           'kind': 'heading' if value.lstrip().startswith('#') else 'paragraph_fragment'})
    ref = context.store.source(kind='novel_facts', path=str(target), sha256=digest,
        start=offset, end=end, total_chars=info['total_chars'], facts=facts, extraction='heuristic_mentions_and_explicit_ages')
    return {'path': context.access.display_path(target), 'sha256': digest, 'source_refs': [ref],
            'paragraphs': paragraphs, 'facts': facts, 'assessment': 'heuristic_candidates_only',
            'coverage': {'start': offset, 'end': end, 'total_chars': info['total_chars'], 'end_reached': end == info['total_chars']},
            'next': {**args, 'offset': end, 'expected_sha256': digest} if end < info['total_chars'] else None}


def fact_table(context, group):
    root = context.access.workspace_path(group, must_exist=True, allow_directory=True)
    if not root.is_dir():
        raise ToolError('invalid_path', '事实表范围必须是章节目录')
    table, coverage, versions = {}, {}, {}
    for ref, item in context.store.record.sources.items():
        if item.get('kind') != 'novel_facts':
            continue
        path = Path(item['path'])
        if not path.is_relative_to(root):
            continue
        try:
            current = context.access.workspace_path(path, must_exist=True)
        except (ToolError, OSError):
            continue
        if str(path) not in versions:
            versions[str(path)] = sha(current)
        digest = versions[str(path)]
        if digest != item['sha256']:
            continue
        ranges = coverage.setdefault(str(path), {'total_chars': item['total_chars'], 'ranges': []})
        ranges['ranges'].append([item['start'], item['end']])
        for fact in item['facts']:
            identity = (str(path), fact['offset'], fact['attribute'], fact['entity'])
            table[identity] = {**fact, 'path': context.access.display_path(path), 'source_ref': ref, 'sha256': digest}
    for value in coverage.values():
        merged = []
        for start, end in sorted(value['ranges']):
            if merged and start <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], end)
            else: merged.append([start, end])
        value['ranges'] = merged
        value['covered_chars'] = sum(end-start for start, end in merged)
        value['complete'] = value['covered_chars'] == value['total_chars']
    return list(table.values()), coverage
