"""Complete public-resource paging and retry budgets; all servers below are fixtures."""
import gzip
import io
import json
from pathlib import Path
import socket
import time
from threading import Timer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from genesisai.core.extensions.tools.web.budget import NetworkLimits, active_scope
from genesisai.core.extensions.tools.web.contracts import SearchQuery
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.providers.brave import BraveSearchProvider
from genesisai.core.extensions.tools.web.providers.bing import BingSearchProvider
from genesisai.core.extensions.tools.web.providers.duckduckgo import DuckDuckGoSearchProvider
from genesisai.core.extensions.tools.web.structured_data import extract_structured_blocks
from genesisai.shared.run_control import RunControl
from genesisai.shared.security import ToolError
from genesisai.shared.security import digest
from test_p11_agent_runtime import runtime_env
from test_p12_runtime import call


def public(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]


def fixture_network(body, media='text/html', **limits):
    return Network(resolver=public, transport=lambda *args: (200, {'content-type': media}, body), limits=limits)


def read(runtime, args, identity):
    result = runtime.execute(call('fetch_web_content', args, identity))
    assert result['ok'], result
    if result['data'].get('kind') == 'builtin_tool':
        end = time.monotonic() + 20
        job_id = result['data']['job_id']
        while result['data']['status'] in {'queued', 'running', 'stopping'}:
            assert time.monotonic() < end, result
            result = runtime.execute(call('read_job', {'job_id': job_id, 'wait_seconds': 1}, identity + '_poll_' + str(time.monotonic_ns())))
            assert result['ok'], result
        return result['data']['result']
    return result['data']


def test_complete_html_source_links_and_structured_blocks_survive_cached_pages(runtime_env):
    store, runtime = runtime_env
    products = [{'@type': 'Product', 'name': 'Product ' + str(index), 'offers': {'priceCurrency': 'USD', 'price': str(index + 1)}}
                for index in range(80)]
    # Both the HTML position and individual JSON-LD block exceed the old caps.
    products[0]['description'] = 'x' * 260000
    raw_json = json.dumps(products)
    html = '<html><title>Complete fixture</title><body><article><p>' + ('Document sentence with useful content. ' * 46000)
    html += '</p><p>TAIL_TEXT_42</p></article>'
    html += ''.join(f'<a href="https://other.example/item/{index}">Item {index}</a>' for index in range(350))
    html += '<script type="application/ld+json">' + raw_json + '</script></body></html>'
    runtime.network = fixture_network(html.encode())
    url = 'https://public.test/large'
    first = read(runtime, {'url': url, 'limit': 50}, 'first')
    assert first['total'] > 200000 and first['source_complete']
    tail = read(runtime, {'url': url, 'offset': first['total'] - 80, 'limit': 80}, 'tail')
    assert tail['cached'] and 'TAIL_TEXT_42' in tail['text']
    source = read(runtime, {'url': url, 'section': 'source', 'offset': len(html) - 70, 'limit': 70}, 'source')
    assert source['text'] == html[-70:]
    links = read(runtime, {'url': url, 'section': 'links', 'offset': 300, 'limit': 50}, 'links')
    assert links['total'] == 350 and links['links'][-1]['url'].endswith('/349')
    assert 'https://other.example/item/349' in store.record.run_runtime['candidates']
    same = read(runtime, {'url': url, 'section': 'links', 'include_cross_domain': False}, 'same_origin')
    assert same['total'] == 0
    blocks = read(runtime, {'url': url, 'section': 'structured'}, 'blocks')
    assert blocks['blocks'][0]['characters'] > 250000 and blocks['blocks'][0]['fact_count'] == 160
    facts = read(runtime, {'url': url, 'section': 'structured', 'block': 0, 'fields': ['Price']}, 'facts')
    assert 'Price: USD 80' in facts['text'] and 'Product:' not in facts['text']
    raw = read(runtime, {'url': url, 'section': 'structured_raw', 'block': 0, 'offset': len(raw_json) - 100, 'limit': 100}, 'raw')
    assert raw['text'] == raw_json[-100:]
    source_ref = first['source_refs'][0]
    assert (store.root / 'sources' / (source_ref + '.bin')).read_bytes() == html.encode()
    assert first['raw_source']['bytes'] == len(html.encode())


def test_pdf_pages_after_thirty_and_raw_bytes_are_available(runtime_env):
    from reportlab.pdfgen.canvas import Canvas
    store, runtime = runtime_env
    buffer = io.BytesIO(); canvas = Canvas(buffer)
    for page in range(1, 36):
        canvas.drawString(50, 700, 'NETWORK_PAGE_' + str(page)); canvas.showPage()
    canvas.save()
    runtime.network = fixture_network(buffer.getvalue(), 'application/pdf')
    args = {'url': 'https://public.test/complete.pdf'}
    first = read(runtime, args, 'pdf')
    assert first['document_pages_total'] == 35
    last = read(runtime, {**args, 'section': 'page', 'page': 35}, 'last_page')
    assert 'NETWORK_PAGE_35' in last['text'] and last['cached']
    pages = read(runtime, {**args, 'section': 'document_pages', 'offset': 30, 'limit': 5}, 'page_directory')
    assert [page['page'] for page in pages['pages']] == [31, 32, 33, 34, 35]
    assert (store.root / 'sources' / (first['source_refs'][0] + '.bin')).read_bytes() == buffer.getvalue()


def test_retry_more_than_two_attempts_and_permanent_http_status_is_not_retried():
    calls = []
    def transport(*args):
        calls.append(args)
        return (503, {'retry-after': '0'}, b'busy') if len(calls) < 4 else (200, {'content-type': 'text/plain'}, b'ok')
    network = Network(resolver=public, transport=transport,
                      limits={'total_seconds': 2, 'retry_initial_seconds': .01, 'retry_max_seconds': .02})
    assert network.fetch('https://public.test/recover')['text'] == 'ok' and len(calls) == 4
    calls.clear()
    def denied(*args):
        calls.append(args)
        return 404, {}, b'missing'
    network.transport = denied
    with pytest.raises(ToolError, match='HTTP 404') as error:
        network.fetch('https://public.test/gone')
    assert not error.value.retryable and len(calls) == 1


def test_retry_after_uses_total_budget_and_backoff_is_cancellable():
    calls = []
    def transport(*args):
        calls.append(args)
        return 429, {'retry-after': '30'}, b'busy'
    network = Network(resolver=public, transport=transport, limits={'total_seconds': .15})
    started = time.monotonic()
    with pytest.raises(ToolError, match='预算'):
        network.get('https://public.test/busy')
    assert len(calls) == 1 and time.monotonic() - started < 1
    control = RunControl(lambda *args: None)
    timer = Timer(.1, control.cancel); timer.start()
    network.default_limits = NetworkLimits(total_seconds=5)
    try:
        with pytest.raises(ToolError) as cancelled:
            control.execute(lambda: network.get('https://public.test/busy'))
        assert cancelled.value.error_type == 'user_cancelled'
    finally:
        timer.cancel()


def test_redirects_share_budget_strip_cross_origin_credentials_and_recheck_dns():
    calls = []
    def transport(url, ip, headers):
        calls.append((url, ip, headers))
        if len(calls) == 1:
            return 302, {'location': '/next'}, b''
        if len(calls) == 2:
            return 302, {'location': 'https://other.example/last'}, b''
        return 200, {}, b'ok'
    network = Network(resolver=public, transport=transport)
    assert network.get('https://public.test/', headers={'Authorization': 'secret', 'X-Token': 'secret'}).content == b'ok'
    assert calls[1][2]['Authorization'] == 'secret'
    assert 'Authorization' not in calls[2][2] and 'X-Token' not in calls[2][2]
    assert all(call[1] == '93.184.216.34' for call in calls)
    mixed = lambda *args, **kwargs: [*public(), (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    with pytest.raises(ToolError, match='混合'):
        Network(resolver=mixed, transport=transport).get('https://public.test/')


def test_byte_budgets_configurable_and_decompression_still_bounded():
    body = b'x' * 6000
    network = Network(resolver=public, transport=lambda *args: (200, {'content-encoding': 'gzip'}, gzip.compress(body)),
                      limits={'response_bytes': 1024, 'decoded_bytes': 7000})
    assert network.get('https://public.test/').content == body
    network.default_limits = NetworkLimits(decoded_bytes=5000)
    with pytest.raises(ToolError, match='解压'):
        network.get('https://public.test/')
    network.transport = lambda *args: (200, {'content-encoding': 'gzip'}, gzip.compress(b'first') + gzip.compress(b'last'))
    assert network.get('https://public.test/').content == b'firstlast'
    import brotli
    network.transport = lambda *args: (200, {'content-encoding': 'br'}, brotli.compress(b'x' * 6000))
    with pytest.raises(ToolError, match='解压'):
        network.get('https://public.test/')
    network.default_limits = NetworkLimits(decoded_bytes=7000)
    assert network.get('https://public.test/').content == b'x' * 6000


def test_brave_pages_and_search_schema_share_query_limit(runtime_env):
    store, runtime = runtime_env
    log = store.root / 'search_requests.jsonl'
    def transport(url, ip, headers):
        params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(params) + '\n')
        page = int(params['offset'])
        body = {'query': {'more_results_available': page < 9}, 'web': {'results': [
            {'title': str(index), 'url': f'https://example.com/{index}'} for index in range(page * 20, page * 20 + 20)]}}
        return 200, {'content-type': 'application/json'}, json.dumps(body).encode()
    runtime.providers = [BraveSearchProvider('fixture', client=Network(resolver=public, transport=transport))]
    query = 'word ' * 99 + 'tail'
    assert len(query) == 499 and SearchQuery(query).query == query
    args = {'query': query, 'provider': 'brave', 'offset': 35, 'result_count': 25, 'allowed_domains': ['example.com'], 'country': 'US', 'language': 'en'}
    result = read(runtime, args, 'search_pages')
    requests = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    assert len(result['hits']) == 25 and result['hits'][0]['url'].endswith('/35') and result['hits'][-1]['url'].endswith('/59')
    assert result['next_offset'] == 60 and requests[0]['offset'] == '1' and requests[1]['offset'] == '2'
    assert requests[0]['country'] == 'US' and 'site:example.com' in requests[0]['q']
    rejected = runtime.execute(call('fetch_web_content', {'query': 'x' * 501}, 'long_query'))
    assert not rejected['ok']


def test_invalid_structured_block_retains_raw_content():
    raw = '{' + 'z' * 260000
    blocks = extract_structured_blocks('<script type="application/ld+json">' + raw + '</script>')
    assert blocks[0]['error'] == 'invalid_json_ld' and blocks[0]['raw'] == raw


def test_search_provider_continuations_follow_actual_source_boundaries():
    requests = []
    def duck(request):
        params = dict(request.url.params); requests.append(params)
        start = 3 if params.get('vqd') == 'fixture-cursor' else 0
        body = ''.join(f'<a class="result__a" href="https://example.com/{index}">Result {index}</a>' for index in range(start, start + 3))
        if start == 0:
            body += '<form><input name="q" value="query"><input name="vqd" value="fixture-cursor"><input name="s" value="3"><input type="submit" value="Next"></form>'
        return httpx.Response(200, text=body)
    provider = DuckDuckGoSearchProvider(client=httpx.Client(transport=httpx.MockTransport(duck)))
    batch = provider.search(SearchQuery('query', offset=2), limit=4)
    assert [hit.url for hit in batch.hits] == [f'https://example.com/{index}' for index in range(2, 6)]
    assert batch.next_offset is None and requests[1]['vqd'] == 'fixture-cursor'
    xml = '<rss><channel>' + ''.join(f'<item><title>Item {i}</title><link>https://example.com/{i}</link></item>' for i in range(10)) + '</channel></rss>'
    provider = BingSearchProvider(httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))))
    batch = provider.search(SearchQuery('query', offset=7), limit=8)
    assert len(batch.hits) == 3 and batch.next_offset is None and batch.coverage == 'complete_rss_response_only'


def test_permanent_failure_requires_explicit_refresh_and_then_cache_recovers(runtime_env):
    store, runtime = runtime_env
    class Server:
        def __init__(self, counter):
            self.counter = counter
        def fetch(self, url):
            calls = int(self.counter.read_text()) if self.counter.exists() else 0
            self.counter.write_text(str(calls + 1))
            if calls == 0:
                raise ToolError('fetch_failed', 'HTTP 404')
            return {'url': url, 'title': 'Recovered', 'text': 'now available', 'truncated': False}
    counter = store.root / 'fixture-calls.txt'
    runtime.network = Server(counter)
    url = 'https://public.test/recovered'
    for identity in ('first', 'again'):
        result = runtime.execute(call('fetch_web_content', {'url': url}, identity))
        assert not result['ok']
    assert counter.read_text() == '1'
    assert read(runtime, {'url': url, 'refresh': True}, 'refresh')['text'] == 'now available'
    assert read(runtime, {'url': url}, 'cache_after_refresh')['cached']
    assert counter.read_text() == '2' and url not in store.record.run_runtime['failed_resources']
    invalid = runtime.execute(call('fetch_web_content', {'url': url, 'section': 'page', 'page': 99}, 'invalid_page'))
    assert not invalid['ok'] and invalid['error']['code'] == 'invalid_range'


def test_async_research_accounts_only_final_origin_result_once(runtime_env):
    store, runtime = runtime_env
    url = 'https://public.test/async'
    original = call('fetch_web_content', {'url': url}, 'origin')
    runtime.research.before_execute(original)
    store.record.calls['origin'] = {'executed': True}
    pending = {'ok': True, 'data': {'kind': 'builtin_tool', 'job_id': 'job_async', 'status': 'queued'}}
    runtime.research.after_execute(original, pending)
    assert store.record.run_runtime['usage']['search_fetches'] == 0
    assert not store.record.run_runtime['evidence_refs']
    source = store.source(kind='web', url=url, requested_url=url, title='Async', sha256=digest('done'))
    observed = {'ok': True, 'data': {'kind': 'builtin_tool', 'job_id': 'job_async', 'status': 'completed',
                'origin_call': original, 'operation_error': None,
                'result': {'url': url, 'title': 'Async', 'text': 'done', 'source_refs': [source]}}}
    for identity in ('read_once', 'read_twice'):
        runtime.research.after_execute(call('read_job', {'job_id': 'job_async'}, identity), observed)
    assert store.record.run_runtime['usage']['search_fetches'] == 1
    assert store.record.run_runtime['fetched_count'] == 1
    assert store.record.run_runtime['evidence_refs'] == [source]
    failed = call('fetch_web_content', {'query': 'unavailable'}, 'search_origin')
    store.record.calls['search_origin'] = {'executed': True}
    result = {'ok': False, 'data': {'kind': 'builtin_tool', 'job_id': 'job_failed', 'status': 'completed',
              'origin_call': failed, 'operation_error': {'code': 'search_unavailable', 'message': 'Unavailable', 'retryable': True},
              'result': {'diagnostics': {'all_providers_failed': True, 'fallback_errors': [{'provider': 'fixture', 'code': 'provider_unavailable'}]}}}}
    runtime.research.after_execute(call('read_job', {'job_id': 'job_failed'}, 'read_failure'), result)
    assert store.record.run_runtime['usage']['search_queries'] == 1
    assert store.record.run_runtime['search_diagnostics']['all_providers_failed']


def test_domain_scope_applies_to_redirects_and_never_disables_public_dns_checks():
    calls = []
    def transport(url, ip, headers):
        calls.append(url)
        return 302, {'location': 'https://outside.example/'}, b''
    network = Network(resolver=public, transport=transport, scope={'authorization_id': 'auth_fixture', 'domains': ['public.test']})
    with pytest.raises(ToolError, match='范围授权'):
        network.get('https://public.test/')
    assert calls == ['https://public.test/']
    token = active_scope.set({'authorization_id': 'auth_fixture', 'domains': ['*.example.org']})
    network.transport = lambda *args: (200, {}, b'ok')
    try:
        with pytest.raises(ToolError, match='范围授权'):
            network.get('https://Sub.Example.Org./')
        # The active grant intersects the intrinsic policy; it never replaces it.
        network.scope = {'authorization_id': 'auth_fixture', 'domains': ['*']}
        assert network.get('https://Sub.Example.Org./').content == b'ok'
        with pytest.raises(ToolError, match='范围授权'):
            network.get('https://example.org/')
    finally:
        active_scope.reset(token)
    network.scope = {'authorization_id': 'auth_fixture', 'domains': ['*']}
    network.resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    with pytest.raises(ToolError, match='私网'):
        network.get('https://localhost/')


def test_long_fetch_runs_as_job_and_final_source_is_accounted_once(runtime_env):
    store, runtime = runtime_env
    class Slow:
        def fetch(self, url):
            time.sleep(7)
            return {'url': url, 'title': 'Slow', 'text': 'complete after waiting', 'truncated': False}
    runtime.network = Slow()
    started = time.monotonic()
    result = runtime.execute(call('fetch_web_content', {'url': 'https://public.test/slow'}, 'long_fetch'))
    assert result['ok'] and result['data']['kind'] == 'builtin_tool'
    assert result['data']['status'] in {'queued', 'running'} and time.monotonic() - started < 7
    assert store.record.run_runtime['usage']['search_fetches'] == 0 and not store.record.run_runtime['evidence_refs']
    identity = result['data']['job_id']
    end = time.monotonic() + 15
    while result['data']['status'] in {'queued', 'running'}:
        assert time.monotonic() < end
        result = runtime.execute(call('read_job', {'job_id': identity, 'wait_seconds': 1}, 'poll_' + str(time.monotonic_ns())))
        assert result['ok'], result
    assert result['data']['result']['text'] == 'complete after waiting'
    assert result['data']['origin_call']['id'] == 'long_fetch'
    assert store.record.run_runtime['usage']['search_fetches'] == 1 and store.record.run_runtime['fetched_count'] == 1
    again = runtime.execute(call('read_job', {'job_id': identity}, 'repeat_final'))
    assert again['ok'] and store.record.run_runtime['fetched_count'] == 1


def test_navigation_only_download_is_not_text_evidence_and_cached_body_needs_no_network_approval(runtime_env):
    store, runtime = runtime_env
    runtime.network = fixture_network(b'<html><article><p>Readable factual document body for later inspection.</p></article><a href="/next">Next</a></html>')
    url = 'https://public.test/navigation'
    links = read(runtime, {'url': url, 'section': 'links'}, 'navigation')
    assert links['links'] and not store.record.run_runtime['evidence_refs']
    runtime.set_permission('user')
    body = read(runtime, {'url': url}, 'cached_body')
    assert body['cached'] and 'Readable factual' in body['text']
    assert store.record.run_runtime['evidence_refs'] == body['source_refs']
    assert store.record.run_runtime['usage']['search_fetches'] == 1
