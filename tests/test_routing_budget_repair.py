import json

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.prompt.task_protocols import TaskProtocolSelector, select_initial_profile
from genesisai.core.research import ResearchController
from genesisai.shared.messages import Response, ToolCall
from test_development_continuation import development_env
from test_p11_agent_runtime import FakeModel


@pytest.mark.parametrize('text,profile', [
    ('帮我看看这个项目仓库 https://github.com/deepseek-ai/deepseek-harness', 'web_quick'),
    ('你不是可以上网吗？这是一个GitHub仓库，你怎么会看不见', 'web_quick'),
    ('看看 https://example.org/python/git/docs', 'web_quick'),
    ('修改本地代码，参考 https://example.org/docs', 'local_files'),
    ('读取并修改 DOCX 和 XLSX', 'local_files'),
    ('解释 digital signal', 'direct_answer'),
])
def test_route_uses_task_not_url_substrings(text, profile):
    assert select_initial_profile(text) == profile


def test_mixed_route_and_readonly_intent():
    router = TaskProtocolSelector()
    route = router.route('只讨论本地代码问题，查文档 https://example.org/docs')
    assert {'coding', 'web'} <= set(route['capability_routes'])
    assert not route['execution_requested']
    assert route['constraints']['read_only']
    assert router.route('禁止联网，修复本地代码')['constraints']['network_forbidden']


def test_long_continuation_does_not_override_discussion():
    router = TaskProtocolSelector()
    task = {'status': 'in_progress'}
    assert router.is_development_continuation('继续修复刚才列出的所有问题，并且每一项都要验证测试通过后再进入下一项', task)
    assert not router.is_development_continuation('继续讨论，暂时不要修改代码', task)
    assert not router.is_development_continuation('另一个任务：继续实现新项目', task)


def test_local_fetch_budget_failure_is_local(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    state = ResearchController.empty_state('local_files')
    store.record.run_runtime = state
    runtime.research.register_user_urls('https://example.org/repo')
    call = {'id': 'fetch', 'name': 'fetch_web_content', 'arguments': json.dumps({'url': 'https://example.org/repo'})}
    assert runtime.research.before_execute(call) is None
    state['budget']['search_fetches'] = 0
    result = runtime.execute(call)
    assert result['error']['code'] == 'budget_exhausted'
    assert '未分配' in result['error']['message']
    assert not store.record.calls['fetch']['executed']
    assert state['phase'] == 'explore' and state['stop_reason'] is None
    assert state['usage']['search_fetches'] == 0


def test_explicit_model_limit_survives_route_change(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    Runner(FakeModel(Response(content='你好')), runtime, max_rounds=55).start('你好')
    store.record.run_runtime['usage']['model_calls'] = 4
    runtime.research.switch_profile('web_quick')
    assert store.record.run_runtime['budget']['model_calls'] == 55
    assert store.record.run_runtime['usage']['model_calls'] == 4


def test_github_request_reaches_network_and_returns_evidence(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    class Network:
        calls = 0
        def fetch(self, url):
            self.calls += 1
            return {'url': url, 'title': 'Repository', 'text': 'Repository README', 'truncated': False}
    network = Network()
    runtime.network = network
    def answer(messages):
        ref = store.record.run_runtime['evidence_refs'][0]
        return Response(content=f'仓库介绍 [{ref}]')
    model = FakeModel(Response(tool_calls=[ToolCall('fetch', 'fetch_web_content', json.dumps({'url': 'https://example.org/git/repo'}))], finish_reason='tool_calls'), answer)
    result = Runner(model, runtime).start('看看这个仓库 https://example.org/git/repo')
    assert result['status'] == 'completed'
    assert network.calls == 1
    assert 'fetch_web_content' in {t['function']['name'] for t in model.tools[0]}


def test_chinese_discovery_and_candidate_only(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    matches = runtime.discover_tools('网络搜索 浏览 网页内容读取', 12, activate=False)
    assert matches['candidates'][0]['name'] == 'fetch_web_content'
    assert not matches['available_next_round']
    assert not store.record.run_runtime.get('discovered_tools')
    runtime.discover_tools('fetch_web_content', 1)
    assert 'fetch_web_content' in store.record.run_runtime['discovered_tools']


def test_constraint_applies_to_discovery_and_execution(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    store.record.run_runtime['constraints'] = {'network_forbidden': True, 'read_only': True}
    offered = {d['function']['name'] for d in runtime.definitions()}
    assert 'editor' not in offered and 'fetch_web_content' not in offered and 'run_commands' not in offered
    call = {'id': 'denied', 'name': 'fetch_web_content', 'arguments': json.dumps({'url': 'https://example.org/repo'})}
    result = runtime.execute(call)
    assert result['error']['code'] == 'task_constraint'
    assert not store.record.calls['denied']['executed']


def test_parameter_error_reports_actual_and_range(tmp_path):
    _, _, _, _, runtime = development_env(tmp_path)
    result = runtime.execute({'id': 'bad', 'name': 'discover_tools', 'arguments': json.dumps({'query': 'web', 'limit': 13})})
    assert result['error']['code'] == 'validation_error'
    assert '实际 13' in result['error']['message'] and '1..12' in result['error']['message']


def test_discussion_followup_does_not_authorize_writes(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    router = TaskProtocolSelector()
    previous = router.route('只讨论本地代码，不要修改，禁止联网')
    following = router.route('继续', {'status': 'planned'}, previous)
    assert following['constraints'] == {'read_only': True, 'network_forbidden': True}
    assert not following['development_continuation'] and not following['execution_requested']
    allowed = router.route('继续实现刚才的代码修改，现在允许联网', {'status': 'planned'}, following)
    assert allowed['execution_requested'] and not any(allowed['constraints'].values())


def test_related_followup_with_new_url_keeps_explicit_constraints():
    router = TaskProtocolSelector()
    previous = router.route('只讨论本地代码，禁止联网')
    following = router.route('继续分析上述问题，参考 https://example.org/docs', previous=previous)
    assert following['followup']
    assert following['constraints'] == {'read_only': True, 'network_forbidden': True}
    assert not following['execution_requested']
    separate = router.route('新任务：阅读 https://example.org/another', previous=previous)
    assert not separate['followup'] and not any(separate['constraints'].values())


def test_large_tool_catalog_is_bounded_and_preserves_pending(tmp_path):
    from dataclasses import replace
    from genesisai.core.tools.selection import select_tools
    _, _, _, _, runtime = development_env(tmp_path)
    base = runtime.registry.get('read_files')
    available = [replace(base, name=f'mcp_tool_{i:02}', category='mcp', description='x' * 2000,
                         spec=replace(base.spec, routes=('mcp',))) for i in range(80)]
    state = {'capability_routes': ['mcp'], 'pending_tools': [{'name': 'mcp_tool_79'}]}
    selected = select_tools(available, state)
    assert 1 < len(selected) < 15
    assert 'mcp_tool_79' in {item.name for item in selected}


def test_local_progress_is_not_web_stagnation(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    state = ResearchController.empty_state('web_quick')
    state['protocols'] = ['coding']
    state['usage']['search_fetches'] = 1
    store.record.run_runtime = state
    for _ in range(5):
        runtime.research.note_progress_round()
    assert state['phase'] == 'explore' and state['stop_reason'] is None


def test_routing_constraints_and_consumption_survive_session_restore(tmp_path):
    from genesisai.core.persistence.session import Session
    _, _, _, store, runtime = development_env(tmp_path)
    Runner(FakeModel(Response(content='讨论结果')), runtime, max_rounds=47, max_tools=95).start('只讨论本地代码，禁止联网')
    state = store.record.run_runtime
    state['usage']['search_fetches'] = 2
    store.commit()
    restored = Session(store.root, store.id)
    assert restored.record.run_runtime['constraints'] == {'network_forbidden': True, 'read_only': True}
    assert restored.record.run_runtime['usage']['search_fetches'] == 2
    assert restored.record.run_runtime['budget']['model_calls'] == 47
    assert restored.record.limits['tools'] == 95
    assert restored.record.deadline == store.record.deadline
    restored.record.run_runtime['constraints']['read_only'] = 'false'
    with pytest.raises(ValueError, match='constraints'):
        restored.commit()
