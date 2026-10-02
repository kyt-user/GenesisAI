"""组装 Tool Runtime，并为 Runner 与 CLI 提供单一入口。"""

from __future__ import annotations

import json

from genesisai.runtime.base import ToolContext, Tool
from genesisai.runtime.catalog import ToolCatalog
from genesisai.runtime.executor import ToolExecutor, error_result
from genesisai.runtime.ledger import CallLedger
from genesisai.runtime.loader import ToolLoader
from genesisai.runtime.permissions import PermissionPolicy
from genesisai.runtime.registry import ToolRegistry
from genesisai.capabilities.web.network import Network
from genesisai.capabilities.web.providers.bing import BingSearchProvider
from genesisai.capabilities.web.providers.brave import BraveSearchProvider
from genesisai.capabilities.web.providers.duckduckgo import DuckDuckGoSearchProvider
from genesisai.agent.research import ResearchController


class ToolRuntime:
    """组装 Registry、Catalog、Loader、Executor、Policy 和 Ledger，为 Runner 提供统一工具接口。"""
    def __init__(
        self,
        store,
        access,
        *,
        network=None,
        providers=None,
        confirm_writes: bool = True,
        confirm_search: bool = True,
        confirm_shell: bool = True,
        registry: ToolRegistry | None = None,
        active_limit: int = 8,
    ):
        self.store = store
        self.access = access
        self._network = network or Network()
        self._providers = providers if providers is not None else [
            BraveSearchProvider(client=self._network),
            DuckDuckGoSearchProvider(client=self._network),
            BingSearchProvider(client=self._network),
        ]
        self.registry = registry or ToolRegistry()
        self.loader = ToolLoader()
        self.policy = PermissionPolicy(
            confirm_writes=confirm_writes,
            confirm_search=confirm_search,
            confirm_shell=confirm_shell,
            store=self.store,
        )
        self.catalog = ToolCatalog(
            self.registry,
            self.loader,
            self.store,
            active_limit=active_limit,
        )
        self.ledger = CallLedger(self.store)
        self.context = ToolContext(
            store=self.store,
            access=self.access,
            network=self._network,
            providers=self._providers,
            runtime=self,
        )
        self.executor = ToolExecutor(
            catalog=self.catalog,
            loader=self.loader,
            policy=self.policy,
            ledger=self.ledger,
            context=self.context,
        )
        self.research = ResearchController(self.store)

    @property
    def confirm_writes(self) -> bool:
        return self.policy.confirm_writes

    @confirm_writes.setter
    def confirm_writes(self, value: bool) -> None:
        self.policy.confirm_writes = value

    @property
    def confirm_search(self) -> bool:
        return self.policy.confirm_search

    @confirm_search.setter
    def confirm_search(self, value: bool) -> None:
        self.policy.confirm_search = value

    @property
    def confirm_shell(self) -> bool:
        return self.policy.confirm_shell

    @confirm_shell.setter
    def confirm_shell(self, value: bool) -> None:
        self.policy.confirm_shell = value

    @property
    def network(self):
        return self._network

    @network.setter
    def network(self, value) -> None:
        self._network = value
        if hasattr(self, "context"):
            self.context.network = value

    @property
    def providers(self) -> list:
        return self._providers

    @providers.setter
    def providers(self, value: list) -> None:
        self._providers = value
        if hasattr(self, "context"):
            self.context.providers = value

    def definitions(self) -> list[dict]:
        self.catalog.refresh()
        return self.catalog.definitions()

    def execute(self, call: dict, approved: str | None = None) -> dict:
        self.catalog.refresh()
        # 先校验调用资格，再查询研究缓存或权限。
        try:
            self.executor._validate_call(call)
            prior = self.ledger.prior(call)
            if prior and prior.get("state") in {"succeeded", "failed", "started", "unknown"}:
                return self._validate_result(call, self.executor.execute(call, approved))
            descriptor = self.catalog.descriptor_for_execution(call["name"])
            Tool(name=descriptor.name, description=descriptor.description,
                 parameters=descriptor.spec.parameters)._validate_args(json.loads(call["arguments"]))
        except Exception:
            return self._validate_result(call, self.executor.execute(call, approved))
        blocked = self.research.before_execute(call)
        if blocked is not None:
            result = self._validate_result(call, blocked)
            self.ledger.finish(call, result, 0)
            return self.research.after_execute(call, result)
        result = self.executor.execute(call, approved)
        return self.research.after_execute(call, self._validate_result(call, result))

    @staticmethod
    def _validate_result(call, result):
        if isinstance(result, dict):
            if result.get("pending") is True and isinstance(result.get("preview"), dict):
                return result
            error = result.get("error")
            if result.get("ok") is True and error is None and isinstance(result.get("data"), dict):
                valid = True
            else:
                valid = result.get("ok") is False and isinstance(error, dict) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str)
            if valid:
                normalized = {'call_id': call.get('id', ''), 'data': None, 'error': None,
                              'source_refs': [], 'artifact_refs': [], 'truncated': False, **result}
                if all(isinstance(normalized[key], list) and all(isinstance(ref, str) for ref in normalized[key]) for key in ('source_refs', 'artifact_refs')):
                    return normalized
        return error_result(call, "invalid_result", "工具返回了无效的结果结构")

    def reject(self, call: dict) -> dict:
        return self.executor.reject(call)

    @property
    def permission_mode(self) -> str:
        return self.policy.mode

    def set_permission(self, capability: str, mode: str) -> None:
        self.policy.set_mode(capability, mode)

    def confirmation_message(self, name: str) -> str:
        """根据工具固定 Spec 返回准确的待确认类型。"""
        self.registry.refresh()
        if name in self.registry:
            permission = self.registry.get(name).spec.permission
            if permission == "network":
                return "需要确认公开网络请求"
            if permission == "write":
                return "需要确认输出文件写入"
            if permission == "shell":
                return "需要确认受控命令执行"
        return "需要确认当前操作"

    def reset_task(self) -> None:
        """显式新会话边界使用；普通 Run 不调用。"""
        self.catalog.reset()

    def load_tools(self, names: list[str], *, replace: bool = False) -> list[str]:
        self.catalog.refresh()
        return self.catalog.activate(names, replace=replace)

    def snapshot(self) -> dict[str, list[dict]]:
        self.catalog.refresh()
        return self.catalog.snapshot()

    @staticmethod
    def error(call: dict, code: str, message: str, retryable: bool = False) -> dict:
        return error_result(call, code, message, retryable)

