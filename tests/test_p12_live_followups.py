"""Regressions derived from real provider fallback and cross-run explanations."""
import json

import pytest

from test_p11_agent_runtime import runtime_env, FakeModel, tool_call
from test_p12_runtime import call, query_provider
from genesisai.shared.messages import Response
from genesisai.agent.runner import Runner
from genesisai.shared.security import ToolError
from genesisai.capabilities.web.contracts import SearchError, SearchQuery
from genesisai.capabilities.web.providers.bing import BingSearchProvider
from genesisai.capabilities.web.providers.brave import BraveSearchProvider
from genesisai.capabilities.web.providers.duckduckgo import DuckDuckGoSearchProvider


def test_brave_distinguishes_missing_credentials_without_exposing_key():
    with pytest.raises(SearchError) as failure:
        BraveSearchProvider(api_key='').search(None)
    assert failure.value.reason == 'missing_credentials'
    assert failure.value.error_type == 'provider_auth_error'


def test_provider_reason_is_safe_and_repeated_candidates_are_explicit(runtime_env):
    store, runtime = runtime_env
    provider = query_provider(runtime)
    class Missing:
        name = 'fixture_missing'
        def search(self, *args, **kwargs):
            raise SearchError('provider_auth_error', 'DO_NOT_LOG_SECRET', reason='missing_credentials')
    runtime.providers = [Missing(), provider]
    first = runtime.execute(call('search_query', {'query': 'product'}, 'q1'))
    second = runtime.execute(call('search_query', {'query': 'product price'}, 'q2'))
    assert first['data']['diagnostics']['new_candidates'] == 1
    assert second['data']['diagnostics']['repeated_candidates'] is True
    assert second['data']['fallback_errors'][0]['reason'] == 'missing_credentials'
    assert '没有新增候选' in second['data']['guidance']
    assert 'DO_NOT_LOG_SECRET' not in json.dumps(store.data)
    assert store.data['run_runtime']['phase'] == 'explore'  # existing candidates may still be useful


def test_unknown_provider_reason_cannot_leak_exception_text(runtime_env):
    _, runtime = runtime_env
    provider = query_provider(runtime)
    class Broken:
        name = 'fixture_broken'
        def search(self, *args, **kwargs):
            raise SearchError('provider_unavailable', 'secret', reason='SECRET_REASON')
    runtime.providers = [Broken(), provider]
    result = runtime.execute(call('search_query', {'query': 'fixture'}, 'q'))
    assert 'reason' not in result['data']['fallback_errors'][0]


@pytest.mark.parametrize(
    ('factory', 'failure', 'reason'),
    [
        (lambda client: DuckDuckGoSearchProvider(client=client), ToolError('timeout', 'SECRET', True), 'timeout'),
        (lambda client: DuckDuckGoSearchProvider(client=client), OSError('SECRET'), 'connection_error'),
        (lambda client: BingSearchProvider(client), ToolError('unsafe_url', 'SECRET'), 'endpoint_rejected'),
        (lambda client: BraveSearchProvider(api_key='fixture', client=client), ToolError('invalid_response', 'SECRET'), 'invalid_response'),
    ],
)
def test_provider_transport_failures_have_stable_safe_reasons(factory, failure, reason):
    class Client:
        def get(self, *args, **kwargs):
            raise failure
    with pytest.raises(SearchError) as caught:
        factory(Client()).search(SearchQuery('fixture'))
    assert caught.value.reason == reason
    assert 'SECRET' not in str(caught.value)


@pytest.mark.parametrize(
    ('status', 'code', 'reason', 'retryable'),
    [
        (403, 'access_denied', 'access_denied', False),
        (429, 'provider_rate_limited', 'rate_limit', True),
        (503, 'provider_unavailable', 'http_status', True),
    ],
)
def test_duckduckgo_http_failures_are_classified(status, code, reason, retryable):
    class Client:
        def get(self, *args, **kwargs):
            return type('Result', (), {'status_code': status, 'text': ''})()
    with pytest.raises(SearchError) as caught:
        DuckDuckGoSearchProvider(client=Client()).search(SearchQuery('fixture'))
    assert (caught.value.error_type, caught.value.reason, caught.value.retryable) == (code, reason, retryable)


def test_all_provider_failures_are_persisted_and_traced_without_secrets(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query'])
    class Provider:
        def __init__(self, name, code, reason, retryable):
            self.name, self.code, self.reason, self.retryable = name, code, reason, retryable
        def search(self, *args, **kwargs):
            raise SearchError(self.code, 'DO_NOT_STORE_SECRET', reason=self.reason, retryable=self.retryable)
    runtime.providers = [
        Provider('one', 'provider_unavailable', 'timeout', True),
        Provider('two', 'provider_invalid_response', 'parse_error', False),
    ]
    result = runtime.execute(call('search_query', {'query': 'fixture'}, 'all_failed'))
    diagnostics = store.data['run_runtime']['search_diagnostics']
    trace = (store.root / 'traces' / f'{store.id}.jsonl').read_text(encoding='utf-8')
    assert result['error']['code'] == 'search_unavailable'
    assert diagnostics['all_providers_failed'] is True
    assert diagnostics['fallback_errors'] == [
        {'provider': 'one', 'code': 'provider_unavailable', 'retryable': True, 'reason': 'timeout'},
        {'provider': 'two', 'code': 'provider_invalid_response', 'retryable': False, 'reason': 'parse_error'},
    ]
    assert 'provider_errors' in trace
    assert 'DO_NOT_STORE_SECRET' not in json.dumps(store.data) + trace


def test_failed_url_cannot_bypass_two_attempt_limit_with_new_call_ids(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    runtime.research.register_user_urls('https://example.com/unavailable')
    class Network:
        calls = 0
        def fetch(self, url):
            self.calls += 1
            raise ToolError('fetch_failed', 'HTTP 503', True)
    runtime.network = Network()
    first = runtime.execute(call('search_fetch', {'url': 'https://example.com/unavailable'}, 'failure_1'))
    second = runtime.execute(call('search_fetch', {'url': 'https://example.com/unavailable'}, 'failure_2'))
    third = runtime.execute(call('search_fetch', {'url': 'https://example.com/unavailable'}, 'failure_3'))
    assert first['error']['code'] == second['error']['code'] == 'fetch_failed'
    assert third['error']['code'] == 'retry_exhausted'
    assert runtime.network.calls == 2
    assert store.data['run_runtime']['failed_resources']['https://example.com/unavailable'] == {
        'code': 'fetch_failed', 'retryable': True, 'attempts': 2,
        'reason': 'http_status', 'http_status': 503,
    }
    assert store.data['run_runtime']['candidates']['https://example.com/unavailable']['status'] == 'failed'


def test_failed_candidate_does_not_block_registered_alternative(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    runtime.research.register_user_urls('https://example.com/unavailable https://example.com/price')
    class Network:
        def fetch(self, url):
            if url.endswith('/unavailable'):
                raise ToolError('fetch_failed', 'HTTP 503', True)
            return {'url': url, 'title': 'Price', 'text': 'Mainland China 256GB CNY 4999', 'truncated': False}
    runtime.network = Network()
    assert not runtime.execute(call('search_fetch', {'url': 'https://example.com/unavailable'}, 'bad'))['ok']
    result = runtime.execute(call('search_fetch', {'url': 'https://example.com/price'}, 'good'))
    assert result['ok'] and result['source_refs']
    assert store.data['run_runtime']['stop_reason'] is None


def test_repeated_search_after_exhausted_candidates_forces_external_failure(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query', 'search_fetch'])
    class Provider:
        name = 'fixture'
        def search(self, *args, **kwargs):
            return [{'url': 'https://example.com/unavailable', 'title': 'Unavailable'}]
    class Network:
        def fetch(self, url):
            raise ToolError('fetch_failed', 'HTTP 503', True)
    runtime.providers, runtime.network = [Provider()], Network()
    assert runtime.execute(call('search_query', {'query': 'fixture price'}, 'query_1'))['ok']
    for number in (1, 2):
        assert not runtime.execute(call('search_fetch', {'url': 'https://example.com/unavailable'}, f'fetch_{number}'))['ok']
    repeated = runtime.execute(call('search_query', {'query': 'fixture official price'}, 'query_2'))
    assert repeated['data']['diagnostics']['repeated_candidates'] is True
    assert store.data['run_runtime']['stop_reason'] == 'external_source_unavailable'


def test_all_search_providers_failing_has_distinct_stop_reason(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query'])
    class Provider:
        name = 'fixture'
        def search(self, *args, **kwargs):
            raise SearchError('provider_unavailable', 'secret', retryable=True, reason='connection_error')
    runtime.providers = [Provider()]
    result = runtime.execute(call('search_query', {'query': 'fixture'}, 'query'))
    assert result['error']['code'] == 'search_unavailable'
    assert store.data['run_runtime']['stop_reason'] == 'search_service_unavailable'


def test_previous_run_activity_does_not_include_older_failures(runtime_env):
    store, runtime = runtime_env
    query_provider(runtime)
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': 'https://example.com/guessed'}, 'old_failure')]),
        Response(content='[[PARTIAL]] 尚未取得正文'),
    ), runtime)
    runner.start('查询资料')
    older_id = store.data['run_id']
    runner.model = FakeModel(
        Response(tool_calls=[tool_call('search_query', {'query': 'price'}, 'new_search')]),
        Response(content='[[PARTIAL]] 尚未取得价格正文'),
    )
    runner.start('价格多少')
    last_id = store.data['run_id']
    runner.model = FakeModel(Response(content='上一轮进行了搜索，未取得价格正文。'))
    runner.start('上一轮做了什么')
    outcome = json.loads(runner.model.contexts[0][0].content.split('对用户用自然语言概括，不复制状态字段：\n', 1)[1].split('\n\n', 1)[0])
    assert outcome['run_id'] == last_id != older_id
    assert [item['call_id'] for item in outcome['tool_activity']] == ['new_search']
    assert outcome['tool_activity'][0]['executed'] is True


@pytest.mark.parametrize('repeated', [False, True])
def test_serialized_tool_markup_is_never_executed_or_displayed(runtime_env, repeated):
    store, runtime = runtime_env
    markup = '<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="search_fetch">not-a-call</｜｜DSML｜｜ invoke>'
    runner = Runner(FakeModel(Response(content=markup), Response(content=markup if repeated else '[[PARTIAL]] 没有取得价格正文。')), runtime)
    result = runner.start('价格多少')
    assert result['status'] == ('failed' if repeated else 'partial')
    assert 'DSML' not in result['answer']
    assert store.data['tool_count'] == 0
    assert not any('DSML' in str(item.get('content')) for item in store.data['messages'])
    assert len(runner.model.contexts) == 2
    assert runner.model.tools[1] == []


def test_serialized_tool_recovery_respects_round_limit(runtime_env):
    _, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='<｜DSML｜invoke name="search_fetch">')), runtime, max_rounds=1)
    result = runner.start('查询资料')
    assert result['status'] == 'failed' and 'DSML' not in result['answer']


def test_page_link_discovery_is_same_origin_bounded_and_prefers_main():
    from genesisai.capabilities.web.links import discover_links
    html = '<nav><a href="/account">Account</a></nav><main><a href="/buy">Buy</a><a href="/buy#same">Duplicate</a></main>'
    html += '<a href="https://evil.example/send">Send</a><a href="http://example.com/insecure">Downgrade</a><a href="javascript:alert(1)">JS</a>'
    links = discover_links(html, 'https://example.com/')
    assert [item['url'] for item in links] == ['https://example.com/buy', 'https://example.com/account']
    many = discover_links(''.join(f'<a href="/item/{i}">' + 'x' * 200 + '</a>' for i in range(500)), 'https://example.com/')
    assert len(many) <= 40 and sum(len(item['title']) + len(item['url']) + 40 for item in many) <= 6000


def test_discovered_links_have_provenance_and_still_require_approval(runtime_env):
    store, runtime = runtime_env
    query_provider(runtime)
    runtime.research.register_user_urls('https://example.com/')
    runtime.network.fetch = lambda url: dict(url=url, title='Fixture', text='Fixture ' + url, truncated=False,
                                           links=[{'url': 'https://example.com/buy', 'title': 'Buy'}, {'url': 'https://evil.example/', 'title': 'Bad'}])
    page = runtime.execute(call('search_fetch', {'url': 'https://example.com/'}, 'home'))
    candidate = store.data['run_runtime']['candidates']['https://example.com/buy']
    assert candidate['source_type'] == 'page_link'
    assert candidate['parent_source_ref'] == page['source_refs'][0]
    assert 'https://evil.example/' not in store.data['run_runtime']['candidates']
    runtime.confirm_search = True
    purchase = runtime.execute(call('search_fetch', {'url': 'https://example.com/buy'}, 'buy'))
    assert purchase['pending'] is True


def test_link_discovery_stops_after_two_hops(runtime_env):
    store, runtime = runtime_env
    query_provider(runtime)
    runtime.research.register_user_urls('https://example.com/0')
    runtime.network.fetch = lambda url: dict(url=url, title='Fixture', text='Fixture ' + url, truncated=False,
                                           links=[{'url': 'https://example.com/' + str(int(url[-1]) + 1), 'title': 'Next'}])
    for i in range(3):
        assert runtime.execute(call('search_fetch', {'url': f'https://example.com/{i}'}, f'f{i}'))['ok']
    assert 'https://example.com/3' not in store.data['run_runtime']['candidates']
    cached = runtime.execute(call('search_fetch', {'url': 'https://example.com/2'}, 'cached'))
    assert cached['data']['links'] == []


def test_no_tools_request_explicitly_disables_model_tool_choice():
    from types import SimpleNamespace
    from genesisai.model.providers.deepseek import DeepSeekClient
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='done', tool_calls=None), finish_reason='stop')], usage=None)
    model = object.__new__(DeepSeekClient)
    model.model, model.generation = 'fixture', {}
    model.reasoning_enabled, model.reasoning_effort = True, 'low'
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model.chat([], tools=[])
    model.chat([], tools=None)
    assert calls[0]['tool_choice'] == 'none'
    assert calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert 'tool_choice' not in calls[1]
    assert calls[1]['extra_body'] == {'thinking': {'type': 'enabled'}}


def test_old_link_lists_are_compacted_without_mutating_stored_result(runtime_env):
    from genesisai.prompt.context_budgeter import ContextBudgeter
    from genesisai.prompt.composer import PromptComposer
    store, _ = runtime_env
    payload = {'data': {'links': [{'url': f'https://example.com/{i}', 'title': str(i)} for i in range(30)]}}
    item = {'role': 'tool', 'content': json.dumps(payload)}
    compacted = ContextBudgeter(store, PromptComposer())._compact_message(item, compact_links=True)
    assert len(json.loads(compacted['content'])['data']['links']) == 5
    assert len(json.loads(item['content'])['data']['links']) == 30


def test_current_context_compacts_older_observations_and_reports_cost(runtime_env):
    from genesisai.prompt.context_budgeter import ContextBudgeter
    from genesisai.prompt.composer import PromptComposer
    store, _ = runtime_env
    def observation(character):
        return json.dumps({'ok': True, 'data': {'text': character * 5000, 'links': [], 'hits': []}}, ensure_ascii=False)
    store.data['messages'] = [
        {'role': 'user', 'content': '价格是多少'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'one', 'name': 'search_fetch', 'arguments': '{}'}]},
        {'role': 'tool', 'content': observation('A'), 'tool_call_id': 'one'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'two', 'name': 'search_fetch', 'arguments': '{}'}]},
        {'role': 'tool', 'content': observation('B'), 'tool_call_id': 'two'},
    ]
    messages = ContextBudgeter(store, PromptComposer()).build('web_quick')
    tool_payloads = [json.loads(item.content) for item in messages if item.role == 'tool']
    report = store.data['run_runtime']['context_report']
    assert len(tool_payloads[0]['data']['text']) == 1200
    assert len(tool_payloads[1]['data']['text']) == 4000
    assert report['system_chars'] == sum(report['system_sections'].values())
    assert report['message_chars'] == sum(report['message_role_chars'].values())
    assert report['total_chars'] == report['system_chars'] + report['message_chars']
    assert sum(report['selected_group_chars']) == report['message_chars']


def test_model_trace_contains_only_context_size_report(runtime_env):
    store, runtime = runtime_env
    Runner(FakeModel(Response(content='完成')), runtime).start('简单问题')
    events = [json.loads(line) for line in (store.root / 'traces' / f'{store.id}.jsonl').read_text(encoding='utf-8').splitlines()]
    started = next(item for item in events if item['event'] == 'model_start')
    assert started['context_report']['total_chars'] > 0
    assert set(started['context_report']['message_role_chars']) == {'user', 'assistant', 'tool', 'other'}
    assert '简单问题' not in json.dumps(started['context_report'], ensure_ascii=False)


def test_unrelated_followup_resets_usage_without_old_evidence_forcing_answer(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    runtime.research.register_user_urls('https://example.com/product')
    runtime.network.fetch = lambda url: {'url': url, 'title': 'Product', 'text': 'Product fixture facts', 'truncated': False}
    assert runtime.execute(call('search_fetch', {'url': 'https://example.com/product'}, 'product'))['ok']
    previous_refs = list(store.data['run_runtime']['evidence_refs'])
    store.data['run_runtime']['usage']['search_queries'] = 2
    runner = Runner(FakeModel(Response(content='历史事件回答')), runtime)
    assert runner.start('某历史事件发生在什么时候')['status'] == 'completed'
    assert store.data['run_runtime']['reuse_evidence'] is True
    assert store.data['run_runtime']['evidence_refs'] == previous_refs
    assert store.data['run_runtime']['usage']['search_queries'] == 0
    assert store.data['run_runtime']['stop_reason'] == 'model_answer'


def test_search_service_failure_explanation_uses_latest_stop_reason(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query'])
    class Provider:
        name = 'fixture'
        def search(self, *args, **kwargs):
            raise SearchError('provider_unavailable', 'secret', retryable=True, reason='connection_error')
    runtime.providers = [Provider()]
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_query', {'query': 'current fact'}, 'search')]),
        Response(content='[[PARTIAL]] 搜索服务当前不可用，尚未取得资料。'),
    ), runtime)
    assert runner.start('查询当前事实')['status'] == 'partial'
    failed_run = store.data['run_id']
    runner.model = FakeModel(Response(content='上一轮搜索服务不可用。'))
    assert runner.start('怎么回事')['status'] == 'completed'
    system = runner.model.contexts[0][0].content
    assert failed_run in system and 'search_service_unavailable' in system
    assert 'secret' not in system


def test_two_domains_without_target_field_remain_partial(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query', 'search_fetch'])
    urls = ['https://one.example/info', 'https://two.example/info']
    class Provider:
        name = 'fixture'
        def search(self, *args, **kwargs):
            return [{'url': url, 'title': 'Information'} for url in urls]
    runtime.providers = [Provider()]
    runtime.network.fetch = lambda url: {'url': url, 'title': 'Information', 'text': url + ' specifications only; no price field.', 'truncated': False}
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_query', {'query': 'item price'}, 'query')]),
        Response(tool_calls=[tool_call('search_fetch', {'url': urls[0]}, 'one')]),
        Response(tool_calls=[tool_call('search_fetch', {'url': urls[1]}, 'two')]),
        Response(content='[[PARTIAL]] 两个正文均未列出价格。'),
    ), runtime)
    result = runner.start('这个项目的价格是多少')
    assert result['status'] == 'partial', (result, store.data['run_runtime'])
    assert len(store.data['run_runtime']['evidence_refs']) == 2
    assert store.data['run_runtime']['stop_reason'] == 'evidence_gap'


def test_non_product_research_uses_the_same_evidence_path(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query', 'search_fetch'])
    url = 'https://history.example/event'
    class Provider:
        name = 'fixture'
        def search(self, *args, **kwargs):
            return [{'url': url, 'title': 'Historical event'}]
    runtime.providers = [Provider()]
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Historical event', 'text': 'The fixture event occurred on 2001-02-03.', 'truncated': False}
    def final(_):
        return Response(content='事件发生于 2001-02-03 [' + store.data['run_runtime']['evidence_refs'][-1] + ']')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_query', {'query': 'fixture historical event date'}, 'query')]),
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        final,
    ), runtime)
    result = runner.start('这个历史事件是哪天发生的')
    assert result['status'] == 'completed' and '2001-02-03' in result['answer']
    assert store.data['run_runtime']['fetched_count'] == 1


def test_multi_region_price_evidence_is_available_without_cross_combining(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/price-regions'
    runtime.research.register_user_urls(url)
    runtime.network.fetch = lambda value: {
        'url': value, 'title': 'Regional prices',
        'text': 'Mainland China: 128GB CNY 4999; 256GB CNY 5999. United States: 128GB USD 799; 256GB USD 899.',
        'truncated': False,
    }
    def final(_):
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'中国大陆 128GB 起售价 CNY 4999；256GB 为 CNY 5999 [{ref}]')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        final,
    ), runtime)
    result = runner.start('https://example.com/price-regions 中国大陆起售价和 256GB 价格')
    assert result['status'] == 'completed'
    assert 'CNY 4999' in result['answer'] and 'CNY 5999' in result['answer']
    assert 'USD' not in result['answer']


def test_loaded_tools_persist_across_capability_followup(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_query', 'search_fetch'])
    runner = Runner(FakeModel(Response(content='可查询并读取公开网页。')), runtime)
    runner.start('你能做什么')
    runner.model = FakeModel(Response(content='可以继续使用已加载的网络工具。'))
    runner.start('刚才的网络工具还可用吗')
    offered = {item['function']['name'] for item in runner.model.tools[0]}
    assert {'search_query', 'search_fetch'} <= offered
    assert store.data['run_runtime']['tool_activity'] == []


def test_unsupported_price_is_discarded_before_recovery(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/price'
    runtime.research.register_user_urls(url)
    runtime.network.fetch = lambda value: {
        'url': value, 'title': 'Price', 'text': 'Mainland China price CNY 4999.', 'truncated': False,
    }
    def unsupported(_):
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'中国大陆起售价 CNY 9999 [{ref}]')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        unsupported,
        lambda _: Response(
            content='[[PARTIAL]] 正文仅列出另一个价格，所问价格尚未取得。 [' + store.data['run_runtime']['evidence_refs'][-1] + ']'
        ),
    ), runtime)
    result = runner.start(url + ' 这个产品起售价是多少')
    assert result['status'] == 'partial', (result, store.data['run_runtime'])
    assert store.data['run_runtime']['stop_reason'] == 'unsupported_evidence_claim'
    assert '9999' not in json.dumps(store.data['messages'], ensure_ascii=False)
    assert len(runner.model.contexts) == 3


def test_supported_price_claim_passes_body_validation(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/price'
    runtime.research.register_user_urls(url)
    runtime.network.fetch = lambda value: {
        'url': value, 'title': 'Price', 'text': 'Mainland China price CNY 4,999.', 'truncated': False,
    }
    def final(_):
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'中国大陆起售价 4999 元 [{ref}]')
    result = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]), final,
    ), runtime).start(url + ' 这个产品起售价是多少')
    assert result['status'] == 'completed' and '4999' in result['answer'], (result, store.data['run_runtime'])


def test_price_claim_without_source_reference_is_rejected(runtime_env):
    store, runtime = runtime_env
    runner = Runner(FakeModel(
        Response(content='起售价 CNY 9999'),
        Response(content='[[PARTIAL]] 没有已读取正文，无法核实价格。'),
    ), runtime)
    result = runner.start('当前价格是多少')
    assert result['status'] == 'partial'
    assert '9999' not in json.dumps(store.data['messages'], ensure_ascii=False)


def test_explicit_web_intent_preloads_builtin_search_tools(runtime_env):
    store, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='[[PARTIAL]] 尚未查询正文。')), runtime)
    runner.start('查询官网目前列出的产品')
    offered = {item['function']['name'] for item in runner.model.tools[0]}
    assert store.data['run_runtime']['profile'] == 'web_quick'
    assert {'search_query', 'search_fetch'} <= offered
    assert store.data['run_runtime']['tool_activity'] == []


def test_recall_reuses_validated_answer_without_model_or_tool_call(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/price'
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Price', 'text': 'Price: EUR 29.3', 'truncated': False}
    def priced(_):
        return Response(content='价格为 EUR 29.3 [' + store.data['run_runtime']['evidence_refs'][-1] + ']')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]), priced,
    ), runtime)
    runner.start(url + ' 的价格是多少')
    runner.model = FakeModel(Response(content='不应调用'))
    result = runner.start('再说一下刚才查到的起售价')
    assert result['status'] == 'completed'
    assert 'EUR 29.3' in result['answer']
    assert len(runner.model.contexts) == 0
    assert runner.model.tools == []
    assert store.data['run_runtime']['tool_activity'] == []


def test_recall_of_older_named_field_uses_one_tool_free_model_call(runtime_env):
    store, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='标题是 HTTP Semantics。')), runtime)
    runner.start('标准标题是什么')
    runner.model = FakeModel(Response(content='它取代了若干早期 RFC。'))
    runner.start('它取代了什么')
    runner.model = FakeModel(Response(content='标题是 HTTP Semantics。'))
    result = runner.start('再说一下刚才查到的标准标题')
    assert result['status'] == 'completed' and 'HTTP Semantics' in result['answer']
    assert len(runner.model.contexts) == 1
    assert runner.model.tools == [[]]
    assert store.data['run_runtime']['tool_activity'] == []


def test_invalid_source_answer_is_not_saved_and_recovery_gets_allowed_refs(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/facts'
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Facts', 'text': 'Verified fixture fact.', 'truncated': False}
    def recovered(_):
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'已核实事实 [{ref}]')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        Response(content='错误引用 [src_0000000000000000]'),
        recovered,
    ), runtime)
    result = runner.start(url + ' 查询事实')
    assert result['status'] == 'completed'
    assert 'src_0000000000000000' not in json.dumps(store.data['messages'])
    assert store.data['run_runtime']['evidence_refs'][0] in runner.model.contexts[-1][0].content


def test_repeated_invalid_sources_end_with_readable_evidence_fallback(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/facts'
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Facts', 'text': 'Verified fixture fact.', 'truncated': False}
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        Response(content='错误引用 [src_0000000000000000]'),
        Response(content='仍然错误 [src_1111111111111111]'),
    ), runtime)
    result = runner.start(url + ' 查询事实')
    assert result['status'] == 'partial'
    assert '无法验证的来源引用' in result['answer']
    assert store.data['run_runtime']['evidence_refs'][0] in result['answer']


def test_structured_product_price_is_preserved_as_bounded_source_text():
    from genesisai.capabilities.web.structured_data import extract_product_facts
    html = '''
    <meta property="product:price:amount" content="999.00">
    <meta property="product:price:currency" content="USD">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Fixture Board",
     "offers":{"@type":"Offer","priceCurrency":"EUR","price":"29.30","availability":"https://schema.org/InStock"}}
    </script>
    <script type="application/ld+json">{"@type":"Article","name":"Ignore","price":"12345"}</script>
    '''
    facts = extract_product_facts(html)
    assert facts.splitlines() == ['Product: Fixture Board', 'Price: EUR 29.30', 'Availability: InStock']
    assert '999' not in facts and '12345' not in facts


def test_structured_product_meta_price_is_used_only_as_fallback():
    from genesisai.capabilities.web.structured_data import extract_product_facts
    html = '<meta property="og:title" content="Fixture"><meta property="product:price:amount" content="49.99"><meta property="product:price:currency" content="USD">'
    assert extract_product_facts(html).splitlines() == ['Product: Fixture', 'Price: USD 49.99']


def test_verified_price_can_complete_after_exploration_reserve(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/price'
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Price', 'text': 'Price: EUR 29.3', 'truncated': False}
    def final(_):
        runtime.research.force_answer('exploration_token_reserve')
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'官方价格为 EUR 29.3 [{ref}]')
    result = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]), final,
    ), runtime).start(url + ' 的价格是多少')
    assert result['status'] == 'completed'
    assert store.data['run_runtime']['stop_reason'] == 'model_answer'


def test_same_number_in_wrong_currency_fails_price_validation(runtime_env):
    store, runtime = runtime_env
    runtime.load_tools(['search_fetch'])
    url = 'https://example.com/regions'
    runtime.network.fetch = lambda value: {
        'url': value, 'title': 'Regional prices',
        'text': 'United States: USD 799. Mainland China: CNY 5999.', 'truncated': False,
    }
    def wrong_currency(_):
        ref = store.data['run_runtime']['evidence_refs'][-1]
        return Response(content=f'中国大陆价格为 CNY 799 [{ref}]')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('search_fetch', {'url': url}, 'fetch')]),
        wrong_currency,
        lambda _: Response(content='[[PARTIAL]] 无法从正文确认所问币种的金额。 [' + store.data['run_runtime']['evidence_refs'][-1] + ']'),
    ), runtime)
    result = runner.start(url + ' 中国大陆价格是多少')
    assert result['status'] == 'partial'
    assert 'CNY 799' not in json.dumps(store.data['messages'], ensure_ascii=False)
    assert store.data['run_runtime']['stop_reason'] == 'unsupported_evidence_claim'

