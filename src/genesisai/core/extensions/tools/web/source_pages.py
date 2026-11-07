"""The same complete source paging contract for first reads and research cache hits."""
from urllib.parse import urlsplit

from genesisai.shared.security import ToolError


def page_source(page, ref, args, *, cached=False):
    section = args.get('section', 'text')
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    links = page.get('links', [])
    if not args.get('include_cross_domain', True):
        links = [link for link in links if urlsplit(link['url'])[:2] == urlsplit(page['url'])[:2]]
    blocks, pages = page.get('structured_blocks', []), page.get('document_pages', [])
    result = {'url': page['url'], 'title': page['title'], 'source_refs': [ref], 'cached': cached,
              'section': section, 'offset': offset, 'text': '', 'links': links[:10] if section == 'text' else [],
              'links_total': len(links), 'links_next_offset': 10 if len(links) > 10 and section == 'text' else None,
              'structured_blocks_total': len(blocks), 'document_pages_total': len(pages),
              'source_complete': not page.get('truncated', False), 'extraction': page.get('extraction'),
              'raw_source': page.get('raw_source'), 'query': None}
    if section == 'links':
        values = links
        limit = min(limit, 1000)
        result['links'] = values[offset:offset + limit]
    elif section == 'structured' and 'block' not in args:
        values = [{'block': block['index'], 'kind': block['kind'], 'characters': len(block['raw']),
                   'fact_count': len(block['facts']), 'error': block['error']} for block in blocks]
        limit = min(limit, 1000)
        result['blocks'] = values[offset:offset + limit]
    elif section in {'structured', 'structured_raw'}:
        index = args.get('block')
        if type(index) is not int or not 0 <= index < len(blocks):
            raise ToolError('invalid_range', '结构化块编号不在源文件范围内')
        block = blocks[index]
        facts = block['facts']
        fields = args.get('fields', [])
        if fields:
            facts = [fact for fact in facts if fact.split(':', 1)[0] in fields]
        values = block['raw'] if section == 'structured_raw' else '\n'.join(facts)
        result.update(block=index, block_kind=block['kind'], block_error=block['error'], text=values[offset:offset + limit])
    elif section == 'document_pages':
        values = pages
        limit = min(limit, 1000)
        result['pages'] = [{'page': page['page'], 'characters': len(page['text'])} for page in pages[offset:offset + limit]]
    elif section == 'page':
        number = args.get('page')
        if type(number) is not int or not 1 <= number <= len(pages):
            raise ToolError('invalid_range', 'PDF页码不在源文件范围内')
        values = pages[number - 1]['text']
        result.update(page=number, text=values[offset:offset + limit])
    elif section in {'text', 'source'}:
        values = page.get('source_text') if section == 'source' else page['text']
        if values is None:
            raise ToolError('unsupported_format', '二进制源请使用文档页读取；完整源已保留于 raw_source')
        result['text'] = values[offset:offset + limit]
    else:
        raise ToolError('validation_error', '未知网页读取区域')
    result.update(total=len(values), next_offset=offset + limit if offset + limit < len(values) else None,
                  truncated=offset + limit < len(values))
    result['read_more'] = {'url': page['url'], 'section': section,
                           **({key: args[key] for key in ('block', 'page', 'fields') if key in args}),
                           'offset': result['next_offset']} if result['next_offset'] is not None else None
    return result
