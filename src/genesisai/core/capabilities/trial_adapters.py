"""Explicit host registrations for rebuilding services inside a Skill trial.

Only importable factory names are persisted. Production instances, bound methods,
closures and their credentials are never part of the trial bootstrap.
"""
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
import inspect

from genesisai.core.tools.base import HostBoundTool
from genesisai.shared.security import ToolError


@dataclass(frozen=True)
class TrialServices:
    runtime: object
    workspace: Path
    permissions: object


def factory(entrypoint):
    if not isinstance(entrypoint, str) or entrypoint.count(':') != 1:
        raise ValueError('Trial adapter 必须为 module:function 导入路径')
    module, name = entrypoint.split(':')
    if not module or not name.isidentifier():
        raise ValueError('Trial adapter 必须为模块顶层函数')
    implementation = getattr(import_module(module), name)
    if not inspect.isfunction(implementation) or implementation.__closure__ or implementation.__qualname__ != name:
        raise ValueError('Trial adapter 不能是绑定方法、闭包或服务实例')
    if implementation.__module__ != module or len(inspect.signature(implementation).parameters) != 1:
        raise ValueError('Trial adapter 只接收一个隔离 TrialServices 参数')
    return implementation


class TrialAdapterRegistry:
    def __init__(self):
        self.providers = {}

    def register(self, name, entrypoint):
        if not isinstance(name, str) or not name or name in self.providers:
            raise ValueError('Trial adapter 工具名称无效或已注册')
        factory(entrypoint)
        self.providers[name] = entrypoint

    def create(self, descriptor, runtime):
        entrypoint = self.providers.get(descriptor.name)
        if entrypoint is None:
            raise ToolError('capability_dependency', '依赖缺少隔离宿主适配器：' + descriptor.name)
        spec = descriptor.spec
        services = TrialServices(runtime, Path(runtime.store.workspace), MappingProxyType({
            'permission': spec.permission, 'network': spec.network,
            'side_effect': spec.side_effect, 'path_scope': spec.path_scope,
        }))
        implementation = factory(entrypoint)(services)
        if not isinstance(implementation, HostBoundTool):
            raise ToolError('capability_dependency', 'Trial adapter 必须返回绑定隔离服务的 HostBoundTool')
        return implementation


def adapters(runtime):
    registry = getattr(runtime, 'trial_adapters', None)
    if registry is None:
        registry = runtime.trial_adapters = TrialAdapterRegistry()
    return registry


class IsolatedMemorySearch(HostBoundTool):
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return {'matches': self.host_service.search(args['query'], corpus='project')}


def memory_search(services):
    """Reference adapter: search only the fresh trial's own MemoryService."""
    if services.permissions['network'] or services.permissions['side_effect']:
        raise ToolError('capability_dependency', '隔离记忆查询适配器仅接受本地只读声明')
    return IsolatedMemorySearch(services.runtime.memory)
