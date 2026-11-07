"""Trusted durable operation adapters. Bootstrap data is passed in memory, never replayed."""

import base64
from copy import copy, deepcopy
from dataclasses import asdict
from pathlib import Path
import os
import sys
from threading import RLock

import cloudpickle

from genesisai.shared.security import ToolError


_SERIALIZE_LOCK = RLock()


def portable_adapter(adapter, *, modules=None):
    if modules is None:
        classes = adapter.__mro__ if isinstance(adapter, type) else type(adapter).__mro__
        names = {getattr(adapter, '__module__', type(adapter).__module__), *(cls.__module__ for cls in classes if cls is not object)}
        modules = [sys.modules[name] for name in names if name != 'builtins' and name in sys.modules]
    with _SERIALIZE_LOCK:
        registered = [module for module in modules if module.__name__ not in cloudpickle.list_registry_pickle_by_value()]
        for module in registered:
            cloudpickle.register_pickle_by_value(module)
        try:
            return base64.b64encode(cloudpickle.dumps(adapter)).decode('ascii')
        finally:
            for module in registered:
                cloudpickle.unregister_pickle_by_value(module)


def snapshot_model(model):
    if model is None:
        return None
    from genesisai.model.openai_compatible import OpenAICompatibleClient
    if isinstance(model, OpenAICompatibleClient):
        return {'kind': 'provider', 'provider': model.provider, 'model': model.model,
                'base_url': str(model.base_url), 'api_key': model.api_key,
                'generation': model.generation, 'context': getattr(model, 'context_config', {}),
                'retry': getattr(model, 'retry_config', {}), 'request_timeouts': model.request_timeouts}
    # Host-supplied test/custom adapters can opt into process isolation by being serializable.
    adapter = copy(model)
    for field in ('io_logger', 'image_resolver'):
        if hasattr(adapter, field):
            setattr(adapter, field, None)
    try:
        return {'kind': 'adapter', 'data': portable_adapter(adapter)}
    except Exception as exc:
        raise ToolError('model_isolation', '此模型适配器无法在独立作业中重建') from exc


def restore_model(snapshot):
    if snapshot is None:
        return None
    if snapshot['kind'] == 'adapter':
        return cloudpickle.loads(base64.b64decode(snapshot['data']))
    from types import SimpleNamespace
    from genesisai.model.config import build_model
    return build_model(settings=SimpleNamespace(**{k: v for k, v in snapshot.items() if k != 'kind'}))[0]


def restore_server(snapshot):
    from genesisai.core.extensions.mcp.bridge import McpServerSpec
    return McpServerSpec(**{**snapshot, 'args': tuple(snapshot.get('args', []))})


def runtime_snapshot(runtime, package):
    dependencies, isolated_services = [], []
    from genesisai.core.tools.base import HostBoundTool
    from genesisai.core.capabilities.trial_adapters import adapters
    from genesisai.shared.skill_graph import all_tools
    names = all_tools(package['definition']['stages']) if package['kind'] == 'skill' else set()
    for name in names:
        if name not in runtime.registry:
            continue
        descriptor = runtime.registry.get(name)
        if descriptor.category == 'mcp':
            continue
        implementation = runtime.loader.load(descriptor)
        if isinstance(implementation, HostBoundTool):
            entrypoint = adapters(runtime).providers.get(name)
            if entrypoint is not None:
                isolated_services.append((descriptor, entrypoint))
        elif descriptor.source == 'external':
            dependencies.append((descriptor, implementation))
    packages = [package, *(item['package'] for item in runtime.capabilities.store.active())]
    variable_names = {name for package in packages if package['kind'] in {'mcp', 'tool'}
                      for name in {**package['definition'].get('env_from', {}), **package['definition'].get('headers_from', {})}.values()}
    return {'state_root': str(runtime.store.root), 'project_id': runtime.store.record.project_id,
            'workspace': str(runtime.store.workspace), 'constraints': dict(runtime.store.record.run_runtime.get('constraints', {})),
            'mcp': [asdict(spec) for spec in runtime.mcp.config.values()],
            'mcp_cache': deepcopy(runtime.mcp.cache),
            'dependencies': base64.b64encode(cloudpickle.dumps(dependencies)).decode('ascii'),
            'trial_adapters': base64.b64encode(cloudpickle.dumps(isolated_services)).decode('ascii'),
            'environment': {name: os.environ[name] for name in variable_names if name in os.environ},
            'model': snapshot_model(runtime.capabilities.model) if package['kind'] == 'skill' else None}


def execute(operation, data, job_root):
    if operation == 'builtin_tool':
        from genesisai.core.jobs.tool_operation import execute_tool
        return execute_tool(data, job_root)
    from genesisai.core.capabilities.execution import run_tool
    if operation == 'external_tool':
        from genesisai.core.capabilities.assets import verify_bundle
        verify_bundle(data['bundle'], data['package']['files'])
        return run_tool(data['package'], data['bundle'], data['arguments'], data['workspace'], job_root / 'logs',
                        environment=data['environment'], owner=data['owner'], result_max_bytes=data['result_max_bytes'], redaction_secrets=data['redaction_secrets'])
    if operation == 'mcp_request':
        from genesisai.core.extensions.mcp.bridge import McpBridge, McpServerSpec
        from genesisai.core.tools.loader import ToolLoader
        from genesisai.core.tools.registry import ToolRegistry
        spec = restore_server(data['server'])
        factory = cloudpickle.loads(base64.b64decode(data['transport_factory'])) if data.get('transport_factory') else None
        bridge = McpBridge(job_root, ToolRegistry(), ToolLoader(), transport_factory=factory).configure({}, servers=[spec])
        try:
            bridge.ensure_connected(spec.name)
            if data['method'] == 'server/discover':
                from dataclasses import asdict
                from genesisai.shared.security import digest
                config_digest = digest(asdict(spec))
                return {'server': spec.name, 'method': 'server/discover', 'config_digest': config_digest,
                        'response': bridge.cache[config_digest]}
            if data['method'] == 'tools/call':
                name = bridge._tool_name(spec.name, data['params']['name'])
                if name not in bridge.registry:
                    raise ToolError('mcp_unavailable', '服务已移除该工具')
                bridge.registry.get(name).validate_arguments(data['params'].get('arguments', {}))
            return {'server': spec.name, 'method': data['method'],
                    'selector': data['params'].get('name', data['params'].get('uri')),
                    'response': bridge._request(spec.name, data['method'], data['params'])}
        finally:
            bridge.close()
    if operation not in {'capability_test', 'capability_try'}:
        raise ToolError('job_operation', '独立宿主不支持此操作')
    from genesisai.core.persistence.session import Session
    from genesisai.shared.security import Access
    from genesisai.core.tools.tool_runtime import ToolRuntime
    from genesisai.core.capabilities.store import CapabilityStore
    from genesisai.core.extensions.mcp.bridge import McpServerSpec
    snapshot = data['runtime']
    os.environ.update(snapshot['environment'])
    workspace = job_root / 'workspace'
    workspace.mkdir(exist_ok=True)
    session = Session(job_root / 'session', workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.record.run_runtime['constraints'] = snapshot['constraints']
    session.commit()
    runtime = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), providers=[], permission_mode='auto')
    runtime.access.host_access = False
    try:
        runtime.capabilities.store = CapabilityStore(snapshot['state_root'], snapshot['project_id'])
        runtime.refresh_capabilities()
        session.record.mcp_tools = snapshot['mcp_cache']
        runtime.mcp.configure(session.record.mcp_tools, servers=[restore_server(spec) for spec in snapshot['mcp']])
        for descriptor, implementation in cloudpickle.loads(base64.b64decode(snapshot['dependencies'])):
            if descriptor.name not in runtime.registry:
                runtime.register_tool(descriptor, implementation)
        from genesisai.core.capabilities.trial_adapters import adapters
        for descriptor, entrypoint in cloudpickle.loads(base64.b64decode(snapshot['trial_adapters'])):
            adapters(runtime).register(descriptor.name, entrypoint)
            if descriptor.name not in runtime.registry:
                runtime.register_tool(descriptor, adapters(runtime).create(descriptor, runtime))
        model = restore_model(snapshot['model'])
        if model is not None:
            runtime.bind_model(model)
        if operation == 'capability_test':
            return runtime.capabilities._test_now(data['args'], progress_path=job_root / 'progress.json')
        return runtime.capabilities._try_now(data['args'])
    finally:
        runtime.close()
