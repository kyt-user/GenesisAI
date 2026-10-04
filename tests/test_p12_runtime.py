"""Real-use regressions; all pages and prices here are synthetic fixtures."""
import json
from dataclasses import asdict

import pytest
import time
from io import StringIO
from rich.console import Console
from genesisai.app.terminal_view import CliView
from genesisai.core.tools.permissions import PermissionPolicy
from genesisai.core.research import ResearchController

from test_p11_agent_runtime import runtime_env, FakeModel, tool_call
from genesisai.shared.messages import Response
from genesisai.agent.runner import Runner


def call(name, args, cid):
    return asdict(tool_call(name, args, cid))


def seed_page(runtime, url="https://example.com/product"):
    class Network:
        calls = 0

        def fetch(self, url):
            self.calls += 1
            return dict(url=url, title="Fixture product A", text="Product A specifications without price. " * 30, truncated=False)

    runtime.network = Network()
    runtime.research.register_user_urls(url)
    result = runtime.execute(call("fetch_web_content", {"url": url}, "first"))
    assert result["ok"]
    return url


def test_cached_success_never_crashes_or_requests_approval(runtime_env):
    store, runtime = runtime_env
    url = seed_page(runtime)
    runtime.confirm_search = True
    result = runtime.execute(call("fetch_web_content", {"url": url}, "cached"))
    assert result["ok"] and result["data"]["cached"]
    assert runtime.network.calls == 1
    assert store.data["run_runtime"]["usage"]["search_fetches"] == 1
    assert len(store.data["run_runtime"]["evidence_refs"]) == 1


def test_invalid_url_is_rejected_before_approval(runtime_env):
    _, runtime = runtime_env
    runtime.confirm_search = True
    result = runtime.execute(call("fetch_web_content", {"url": "https://example.com/unknown"}, "bad"))
    assert not result.get("pending")
    assert result["error"]["code"] == "candidate_not_registered"


@pytest.mark.parametrize('prompt', ['其售价多少', '多少钱', '哪个版本便宜', '国行价格呢'])
def test_price_followup_can_search_without_magic_words(runtime_env, prompt):
    store, runtime = runtime_env
    model = FakeModel(Response(content="Product A specifications"))
    runner = Runner(model, runtime)
    runner.start("产品 A 是什么")
    seed_page(runtime)

    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return [{"url": "https://example.com/buy", "title": "Product A price", "snippet": "Fixture"}]

    runtime.providers = [Provider()]
    runner.model = FakeModel(Response(tool_calls=[tool_call("fetch_web_content", {"query": "Product A price"}, "price")]), Response(content="价格尚未核实"))
    runner.start(prompt)
    results = [json.loads(m["content"]) for m in store.data["messages"] if m["role"] == "tool" and m["tool_call_id"] == "price"]
    assert results[0]["ok"]
    assert store.data["run_runtime"]["usage"]["search_queries"] == 1


@pytest.mark.parametrize('payload', [None, [], 'oops', {'ok': True, 'error': []}, {'ok': False, 'error': None}, {'ok': False, 'error': 'oops'}])
def test_invalid_outer_results_are_protocol_errors(runtime_env, monkeypatch, payload):
    _, runtime = runtime_env
    monkeypatch.setattr(runtime.executor, 'execute', lambda *a: payload)
    result = runtime.execute(call('list_files', {'path': '.'}, 'invalid'))
    assert result['error']['code'] == 'invalid_result'


def query_provider(runtime):
    class Provider:
        name = 'fixture'
        calls = 0

        def search(self, query, limit=5):
            self.calls += 1
            return [{'url': 'https://example.com/buy', 'title': 'Fixture purchase page', 'snippet': 'Not evidence'}]

    provider = Provider()
    runtime.providers = [provider]
    return provider


def waiting_runner(runtime):
    runtime.confirm_search = True
    provider = query_provider(runtime)
    model = FakeModel(
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'product A price'}, 'q1')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'product A versions'}, 'q2')]),
        Response(content='[[PARTIAL]] 尚未取得价格正文'),
    )
    runner = Runner(model, runtime)
    assert runner.start('多少钱')['status'] == 'awaiting_confirmation'
    return runner, provider


def test_run_grant_executes_pending_once_and_expires(runtime_env):
    store, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    runtime.policy.allow_network_run()
    assert runner.confirm(True)['status'] == 'partial'
    assert provider.calls == 2
    assert runtime.policy.network_run_id is None
    with pytest.raises(ValueError):
        runner.confirm(True)
    runner.model = FakeModel(Response(tool_calls=[tool_call('fetch_web_content', {'query': 'new question'}, 'q3')]))
    assert runner.start('再问一个问题')['status'] == 'awaiting_confirmation'
    assert provider.calls == 2


def test_session_grant_and_revocation_are_independent(runtime_env):
    store, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    runtime.set_permission('network', 'allow')
    assert runner.confirm(True)['status'] == 'partial'
    assert not runtime.policy.requires_confirmation(runtime.registry.get('fetch_web_content').spec)
    assert runtime.policy.requires_confirmation(runtime.registry.get('run_commands').spec)
    runtime.set_permission('network', 'ask')
    assert runtime.policy.requires_confirmation(runtime.registry.get('fetch_web_content').spec)
    assert provider.calls == 2


def test_wait_does_not_consume_execution_time(runtime_env, monkeypatch):
    import genesisai.agent.runner as runner_module
    now = time.time()
    monkeypatch.setattr(runner_module.time, 'time', lambda: now)
    store, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    deadline = store.data['deadline']
    now += 200
    assert runner.confirm(True)['status'] == 'awaiting_confirmation'
    assert store.data['deadline'] == pytest.approx(deadline + 200)
    assert provider.calls == 1


def test_expired_approval_is_repreviewed_even_with_session_allow(runtime_env, monkeypatch):
    import genesisai.agent.runner as runner_module
    now = time.time()
    monkeypatch.setattr(runner_module.time, 'time', lambda: now)
    _, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    now += 901
    runtime.set_permission('network', 'allow')
    result = runner.confirm(True)
    assert result['status'] == 'awaiting_confirmation'
    assert '过期' in result['answer']
    assert provider.calls == 0
    assert runner.confirm(True)['status'] == 'partial'
    assert provider.calls == 2


def test_run_grant_does_not_restore_or_override_writes(runtime_env):
    store, runtime = runtime_env
    runner, _ = waiting_runner(runtime)
    runtime.confirm_writes = True
    runtime.policy.allow_network_run()
    assert runtime.policy.requires_confirmation(runtime.registry.get('editor').spec)
    restored = PermissionPolicy(store=store)
    assert restored.requires_confirmation(runtime.registry.get('fetch_web_content').spec)
    runner.cancel()
    assert runtime.policy.network_run_id is None


@pytest.mark.parametrize('boundary', ['drive', 'confirmation'])
def test_exception_has_safe_diagnostic_and_next_run_recovers(runtime_env, monkeypatch, boundary):
    store, runtime = runtime_env
    runner, _ = waiting_runner(runtime)
    original = runtime.execute
    def broken(*args, **kwargs):
        raise AttributeError('Authorization: Bearer SECRET_CANARY api_key=SECRET_CANARY')
    monkeypatch.setattr(runtime, 'execute', broken)
    if boundary == 'drive':
        result = runner.drive()
    else:
        result = runner.confirm(True)
    assert result['status'] == 'failed'
    assert store.data['run_runtime']['stop_reason'] == 'runtime_error'
    diagnostic = store.data['run_runtime']['last_failure']
    assert diagnostic['type'] == 'AttributeError' and diagnostic['frames']
    trace = (store.root / 'traces' / f'{store.id}.jsonl').read_text(encoding='utf-8')
    assert 'SECRET_CANARY' not in trace
    assert len([m for m in store.data['messages'] if m.get('tool_call_id') == 'q1']) == 1
    monkeypatch.setattr(runtime, 'execute', original)
    runner.model = FakeModel(Response(content='发生内部异常'))
    assert runner.start('怎么回事')['status'] == 'completed'
    assert 'runtime_error' in runner.model.contexts[0][0].content
    assert store.data['run_runtime']['stop_reason'] == 'model_answer'


def test_official_domains_never_force_completion(runtime_env):
    store, runtime = runtime_env
    store.data['run_runtime']['evidence'] = {'x': {'domain': 'apple.com'}, 'y': {'domain': 'example.com'}}
    assert not runtime.research.should_force_answer()


def test_third_same_domain_page_and_cached_offset(runtime_env):
    store, runtime = runtime_env
    url = seed_page(runtime)
    # Different source bodies make three necessary same-domain pages.
    runtime.network.fetch = lambda url: dict(url=url, title='Fixture', text=(url + ' facts ') * 100, truncated=False)
    for index in range(2):
        other = f'https://example.com/page{index}'
        runtime.research.register_user_urls(other)
        assert runtime.execute(call('fetch_web_content', {'url': other}, f'page{index}'))['ok']
    assert len(store.data['run_runtime']['evidence_refs']) == 3
    result = runtime.execute(call('fetch_web_content', {'url': url, 'offset': 10, 'limit': 20}, 'offset'))
    assert result['data']['text'] == ('Product A specifications without price. ' * 30)[10:30]


def test_forced_answer_and_explicit_gap_are_partial(runtime_env):
    _, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='[[PARTIAL]] 未取得币种和容量')), runtime)
    result = runner.start('价格多少')
    assert result['status'] == 'partial' and '[[PARTIAL]]' not in result['answer']


@pytest.mark.parametrize('width', [80, 120])
def test_readable_sources_and_price_table(runtime_env, width):
    store, runtime = runtime_env
    seed_page(runtime)
    ref = store.data['run_runtime']['evidence_refs'][0]
    stream = StringIO()
    view = CliView(Console(file=stream, width=width, force_terminal=False))
    view.render_result({'status': 'partial', 'answer': f'| 容量 | 售价 |\n| --- | --- |\n| 256GB | CNY 4999 |\n\n[{ref}]'}, store.data)
    output = stream.getvalue()
    assert '256GB' in output and '4999' in output and 'CNY' in output
    assert 'Fixture product A' in output and 'src_' not in output
    assert '部分完成' in output
    assert 'https://example.com/product' in output


def test_r1_complete_product_price_followup_and_reuse(runtime_env):
    store, runtime = runtime_env
    events = []
    class Provider:
        name = 'fixture'
        calls = 0
        def search(self, query, limit=5):
            self.calls += 1
            url = 'https://example.com/product' if self.calls == 1 else 'https://example.com/buy'
            return [{'url': url, 'title': 'Fixture product A', 'snippet': 'candidate only'}]
    class Network:
        urls = []
        def fetch(self, url):
            self.urls.append(url)
            text = 'Product A: synthetic specifications.' if url.endswith('/product') else 'Product A: China mainland, 256GB, starting at CNY 4999. 512GB CNY 5999.'
            return dict(url=url, title='Fixture product A', text=text, truncated=False)
    provider, network = Provider(), Network()
    runtime.providers, runtime.network = [provider], network
    runtime.confirm_search = True
    def answer(text):
        return lambda _: Response(content=text + ' [' + store.data['run_runtime']['evidence_refs'][-1] + ']')
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'Product A specifications'}, 'r1q')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'url': 'https://example.com/product'}, 'r1f')]),
        answer('Product A synthetic specifications'),
    ), runtime, on_event=lambda kind, value: events.append((kind, value)))
    assert runner.start('看看产品 A')['status'] == 'awaiting_confirmation'
    runtime.set_permission('network', 'allow')
    assert runner.confirm(True)['status'] == 'completed'
    runner.model = FakeModel(
        Response(tool_calls=[tool_call('fetch_web_content', {'url': 'https://example.com/buy'}, 'r1bad')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'Product A price'}, 'r2q')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'url': 'https://example.com/buy'}, 'r2f')]),
        answer('中国大陆 256GB 起售价 CNY 4999；512GB CNY 5999'),
    )
    result = runner.start('其售价多少')
    assert result['status'] == 'completed' and '4999' in result['answer']
    assert store.data['calls']['r1bad']['result']['error']['code'] == 'candidate_not_registered'
    assert not store.data['calls']['r1bad']['executed']
    runner.model = FakeModel(answer('中国大陆 256GB 起售价 CNY 4999'))
    assert runner.start('再说一下起售价')['status'] == 'completed'
    assert provider.calls == 2
    assert network.urls == ['https://example.com/product', 'https://example.com/buy']
    assert store.data['tool_count'] == 0
    assert store.data['run_runtime']['usage']['search_fetches'] == 0
    assert store.data['run_runtime']['fetched_count'] == 0
    assert runner.model.tools == []


@pytest.mark.parametrize('approve', ['确认', '同意', '/allow network run', '/allow network'])
def test_cli_network_approval_paths(tmp_path, monkeypatch, approve):
    import genesisai.app.cli as cli
    from test_acceptance import FakeModel as CliModel
    model = CliModel(
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'fixture'}, 'q')]),
        Response(content='[[PARTIAL]] 未抓取正文'),
    )
    stream = StringIO()
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=stream, width=100, force_terminal=False))
    monkeypatch.setattr(cli, 'build_model', lambda path: (model, False))
    original = cli.build_cli_session
    captured = []
    def build(*a, **kw):
        state = original(*a, **kw)
        query_provider(state.runtime)
        captured.append(state)
        return state
    monkeypatch.setattr(cli, 'build_cli_session', build)
    commands = iter(['查产品价格', '确认，重新开始搜索', approve, '/exit'])
    monkeypatch.setattr('rich.console.Console.input', lambda *a, **kw: next(commands))
    assert cli.main(['--workspace', str(tmp_path / 'workspace')]) == 0
    assert captured[0].store.data['calls']['q']['state'] == 'succeeded'
    assert '其他语句不会自动批准' in stream.getvalue()
    assert captured[0].runtime.providers[0].calls == 1


def test_frozen_parameters_cannot_change_after_approval(runtime_env):
    store, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    # The request remains frozen even after switching session permission to allow.
    runtime.set_permission('network', 'allow')
    store.data['pending'][0]['arguments'] = json.dumps({'query': 'changed'})
    result = runner.confirm(True)
    tool_results = [json.loads(m['content']) for m in store.data['messages'] if m.get('tool_call_id') == 'q1']
    assert tool_results[0]['error']['code'] in {'duplicate_call_id', 'confirmation_invalid'}
    assert provider.calls == 1  # only the later, independent q2


def test_repeated_bad_url_cannot_issue_network(runtime_env):
    _, runtime = runtime_env
    runtime.confirm_search = True
    for i in range(3):
        result = runtime.execute(call('fetch_web_content', {'url': 'https://example.com/missing'}, f'bad{i}'))
        assert not result.get('pending') and result['error']['code'] == 'candidate_not_registered'


@pytest.mark.parametrize('reason', ['deadline', 'failure_limit', 'search_fetch_budget'])
def test_forced_stop_reason_is_preserved(runtime_env, reason):
    store, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='部分资料')), runtime)
    def force(messages):
        runtime.research.force_answer(reason)
        return Response(content='部分资料')
    runner.model = FakeModel(force)
    result = runner.start('查询资料')
    assert result['status'] == 'partial'
    assert store.data['run_runtime']['stop_reason'] == reason


def test_success_result_optional_fields_are_normalized(runtime_env, monkeypatch):
    _, runtime = runtime_env
    monkeypatch.setattr(runtime.research, 'before_execute', lambda c: {'ok': True, 'data': {'cached': True}})
    result = runtime.execute(call('list_files', {'path': '.'}, 'minimal'))
    assert result['ok'] and result['error'] is None and result['source_refs'] == []


def test_search_budget_does_not_cancel_fetch_after_second_query(runtime_env):
    store, runtime = runtime_env
    query_provider(runtime)
    for i in range(2):
        assert runtime.execute(call('fetch_web_content', {'query': f'query {i}'}, f'query{i}'))['ok']
    assert store.data['run_runtime']['phase'] == 'explore'
    runtime.network.fetch = lambda url: dict(url=url, text='Fixture CNY 4999', title='Fixture', truncated=False)
    assert runtime.execute(call('fetch_web_content', {'url': 'https://example.com/buy'}, 'fetch'))['ok']


def test_new_session_resets_startup_preapproval(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    model = FakeModel(Response(content='你好'))
    monkeypatch.setattr(cli, 'build_model', lambda path: (model, False))
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=StringIO(), force_terminal=False))
    commands = iter(['/new', '/exit'])
    monkeypatch.setattr('rich.console.Console.input', lambda *a, **kw: next(commands))
    captured = []
    original = cli.build_cli_session
    def build(*a, **kw):
        state = original(*a, **kw)
        captured.append(state)
        return state
    monkeypatch.setattr(cli, 'build_cli_session', build)
    assert cli.main(['--workspace', str(tmp_path / 'workspace'), '--yes-search', '--yes-shell', '--yes-writes']) == 0
    assert not captured[0].runtime.confirm_search
    assert captured[1].runtime.confirm_search and captured[1].runtime.confirm_writes and captured[1].runtime.confirm_shell


def test_actual_capabilities_are_in_model_context(runtime_env):
    _, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='能力取决于工具目录')), runtime)
    runner.start('你能做什么')
    text = runner.model.contexts[0][0].content
    assert '工具能力状态' in text and 'available' in text and 'fetch_web_content' in text


def test_failed_public_page_is_bounded_and_partial(runtime_env):
    from genesisai.shared.security import ToolError
    store, runtime = runtime_env
    query_provider(runtime)
    def missing(url):
        raise ToolError('http_error', 'HTTP 404', True)
    runtime.network.fetch = missing
    runner = Runner(FakeModel(
        Response(tool_calls=[tool_call('fetch_web_content', {'query': 'Product A'}, 'q')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'url': 'https://example.com/buy'}, 'f')]),
        Response(tool_calls=[tool_call('fetch_web_content', {'url': 'https://example.com/buy'}, 'f2')]),
        Response(content='[[PARTIAL]] 购买页不可用，售价未核实'),
    ), runtime)
    result = runner.start('产品 A 多少钱')
    assert result['status'] == 'partial'
    assert store.data['calls']['f2']['result']['error']['code'] == 'retry_exhausted'
    assert store.data['run_runtime']['usage']['search_fetches'] == 1


def test_rejection_has_distinct_stop_reason_and_never_executes(runtime_env):
    store, runtime = runtime_env
    runner, provider = waiting_runner(runtime)
    runner.model = FakeModel(Response(content='已停止查询'))
    result = runner.confirm(False)
    assert result['status'] == 'partial'
    assert store.data['run_runtime']['stop_reason'] == 'user_rejected'
    assert provider.calls == 0


def test_cached_new_spans_count_as_progress_but_duplicates_do_not(runtime_env):
    store, runtime = runtime_env
    url = seed_page(runtime)
    before = store.data['run_runtime']['usage']['observations']
    for i in range(3):
        runtime.execute(call('fetch_web_content', {'url': url, 'offset': 10, 'limit': 20}, f'span{i}'))
    assert store.data['run_runtime']['usage']['observations'] == before + 1


def test_followup_context_keeps_answer_and_compacts_previous_pages(runtime_env):
    store, runtime = runtime_env
    runner = Runner(FakeModel(Response(content='之前的答案')), runtime)
    runner.start('产品规格')
    store.data['messages'].insert(-1, {'role': 'assistant', 'content': None, 'tool_calls': [call('fetch_web_content', {'url': 'https://example.com/fixture'}, 'fixture')]})
    store.data['messages'].insert(-1, {'role': 'tool', 'tool_call_id': 'fixture', 'content': json.dumps({'ok': True, 'data': {'text': 'Z' * 4000}})})
    runner.model = FakeModel(Response(content='继续回答'))
    runner.start('它的价格呢')
    context = runner.model.contexts[0]
    previous = next(json.loads(m.content) for m in context if m.tool_call_id == 'fixture')
    assert len(previous['data']['text']) == 800
    assert any(m.content == '之前的答案' for m in context)

