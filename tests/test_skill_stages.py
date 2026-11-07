from types import SimpleNamespace
import pytest

from genesisai.shared.skill_graph import validate_stages, resolve_stage
from genesisai.core.capabilities.trial_adapters import TrialAdapterRegistry, adapters
from genesisai.core.tools.base import ToolSpec, object_schema
from test_capability_lifecycle import capability_runtime  # noqa: F401 - shared pytest fixture
from genesisai.shared.security import ToolError


def stage(required=(), optional=(), depends=()):
    return {'depends_on': list(depends), 'required_tools': list(required), 'optional_tools': list(optional)}


def test_stage_closure_is_ordered_and_optional_dependencies_are_reported():
    graph = {'inspect': stage(['read_files'], ['missing_ocr']),
             'edit': stage(['editor'], ['read_files'], ['inspect']),
             'publish': stage(['unavailable_release'], depends=['edit'])}
    validate_stages(graph)
    result = resolve_stage(graph, 'edit', lambda name: name in {'read_files', 'editor'})
    assert result == {'stage': 'edit', 'stages': ['inspect', 'edit'], 'tools': ['editor', 'read_files'],
                      'missing_optional_tools': ['missing_ocr']}
    with pytest.raises(ValueError, match='必需工具'):
        resolve_stage(graph, 'publish', lambda name: name in {'read_files', 'editor'})


@pytest.mark.parametrize('graph', [
    {'a': stage(depends=['b']), 'b': stage(depends=['a'])},
    {'a': stage(depends=['absent'])},
    {'a': stage(['reader'], ['reader'])},
    {'a': {'depends_on': [], 'required_tools': ['a', 'a'], 'optional_tools': []}},
    {'a': {'tools': ['reader']}},
])
def test_invalid_dependency_graphs_fail_before_loading(graph):
    with pytest.raises(ValueError):
        validate_stages(graph)


def test_more_than_fifty_tools_and_deep_stages_have_no_recursion_limit():
    graph = {'s0': stage(['tool_' + str(i) for i in range(200)])}
    graph.update({'s' + str(i): stage(depends=['s' + str(i-1)]) for i in range(1, 1100)})
    result = resolve_stage(graph, 's1099', lambda name: True)
    assert len(result['stages']) == 1100 and len(result['tools']) == 200


def test_host_adapter_constructs_only_the_isolated_service(tmp_path):
    production = object()
    isolated = SimpleNamespace(memory=object(), store=SimpleNamespace(workspace=tmp_path))
    descriptor = SimpleNamespace(name='workspace_memory', spec=ToolSpec(parameters=object_schema([]), permission='read', path_scope='authorized'))
    registry = TrialAdapterRegistry()
    registry.register(descriptor.name, 'genesisai.core.capabilities.trial_adapters:memory_search')
    implementation = registry.create(descriptor, isolated)
    assert implementation.host_service is isolated.memory and implementation.host_service is not production
    with pytest.raises(ValueError, match='已注册'):
        registry.register(descriptor.name, 'genesisai.core.capabilities.trial_adapters:memory_search')
    with pytest.raises(ToolError, match='隔离宿主'):
        registry.create(SimpleNamespace(name='absent', spec=descriptor.spec), isolated)
    with pytest.raises(ValueError):
        registry.register('bound', 'genesisai.core.capabilities.trial_adapters:TrialAdapterRegistry.create')


class ProductionOnlyMemory:
    def __init__(self):
        self.calls = 0
    def search(self, *args, **kwargs):
        self.calls += 1
        return [{'secret': 'production-only-marker'}]
    def __reduce__(self):
        raise AssertionError('production service must never be serialized')


def test_actual_skill_job_rebuilds_host_service_without_serializing_production(capability_runtime):
    import json
    from genesisai.core.capabilities.trial_adapters import IsolatedMemorySearch
    from genesisai.core.tools.base import ToolDescriptor
    from genesisai.core.persistence.session import Session
    from genesisai.shared.messages import Response
    from test_capability_lifecycle import skill_package, created, reference, invoke
    from test_tool_result_jobs import finish
    from test_acceptance import FakeModel, call, response
    runtime = capability_runtime
    service = ProductionOnlyMemory()
    name = 'isolated_memory_lookup'
    descriptor = ToolDescriptor(name, True, 'external', 'Search isolated trial memory', runtime.store.workspace,
        ToolSpec(parameters=object_schema(['query'], query={'type':'string'}), permission='read', path_scope='none'),
        source='external', scope='project')
    runtime.register_tool(descriptor, IsolatedMemorySearch(service))
    adapters(runtime).register(name, 'genesisai.core.capabilities.trial_adapters:memory_search')
    package = skill_package()
    package['definition']['stages']['run'] = stage([name])
    package['files']['SKILL.md'] = '使用 isolated_memory_lookup 查询 production，再报告工具返回的内容。'
    package['tests'][0]['input']['prompt'] = '按照 Skill 查询隔离记忆。'
    package['tests'][0]['expect'] = {'properties': {'status': {'const':'completed'}}, 'required':['status']}
    runtime.bind_model(FakeModel(response(call(name, {'query':'production'}, 'lookup')), Response(content='查询完成')))
    item = created(runtime, package)
    _, submitted = invoke(runtime, 'test_capability', reference(item))
    result = finish(runtime, submitted)
    assert result['report']['passed'], result
    trial = result['report']['cases'][0]['result']
    session = Session(trial['state_root'], trial['session_id'])
    calls = [entry for entry in session.record.calls.values() if entry['call']['name'] == name]
    assert calls and calls[0]['result']['data']['matches'] == []
    assert service.calls == 0 and 'production-only-marker' not in json.dumps(result)
