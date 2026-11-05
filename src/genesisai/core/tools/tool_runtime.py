"""组装 Tool Runtime，并为 Runner 与 CLI 提供单一入口。"""

from __future__ import annotations

from copy import copy
import json
import time
from pathlib import Path
from genesisai.shared.ids import uid

from genesisai.core.tools.base import ToolContext, Tool
from genesisai.core.memory.scope import CodeScope
from genesisai.core.tools.catalog import ToolCatalog
from genesisai.core.tools.executor import ToolExecutor, error_result
from genesisai.core.tools.ledger import CallLedger
from genesisai.core.tools.loader import ToolLoader
from genesisai.core.tools.permissions import PermissionPolicy
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.extensions.skills.registry import SkillRegistry
from genesisai.core.extensions.mcp import McpBridge
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.providers.bing import BingSearchProvider
from genesisai.core.extensions.tools.web.providers.brave import BraveSearchProvider
from genesisai.core.extensions.tools.web.providers.duckduckgo import (
    DuckDuckGoSearchProvider,
)
from genesisai.core.research import ResearchController
from genesisai.core.development import DevelopmentPolicy
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.core.prompt.task_protocols import TaskProtocolSelector
from genesisai.core.state.trace import TraceRecorder
from genesisai.core.memory import MemoryService, TaskService
from genesisai.core.memory.project_index import ProjectIndex
from genesisai.core.memory.maintenance import Maintenance
from genesisai.core.tools.observations import ObservationRegistry
from genesisai.core.paths import ApplicationPaths
from genesisai.core.registration import register_installed_extensions
from genesisai.core.media.images import ImageStore
from genesisai.shared.messages import validate_content


class ToolRuntime:
    """组装 Registry、Catalog、Loader、Executor、Policy 和 Ledger，为 Runner 提供统一工具接口。"""

    def __init__(
        self,
        store,
        access,
        *,
        network=None,
        providers=None,
        permission_mode: str | None = None,
        registry: ToolRegistry | None = None,
        logger=None,
    ):
        self.store = store
        self.access = copy(access)
        self.logger = logger or store.get_logger()
        self.images = ImageStore(self.store, self.access)
        self._network = network or Network()
        self._providers = (
            providers
            if providers is not None
            else [
                BraveSearchProvider(client=self._network),
                DuckDuckGoSearchProvider(client=self._network),
                BingSearchProvider(client=self._network),
            ]
        )
        self.registry = registry or ToolRegistry()
        self.loader = ToolLoader()
        self.set_permission(
            permission_mode
            if permission_mode is not None
            else getattr(store.record, "permission_mode", "user")
        )
        self.catalog = ToolCatalog(
            self.registry,
            self.loader,
            self.store,
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
        self.composer = PromptComposer()
        self.context_budgeter = ContextBudgeter(self.store, self.composer)
        self.prompt = TaskProtocolSelector()
        self.trace = TraceRecorder(self.store)
        code_scope = CodeScope(store.workspace, [*access.roots, access.output])
        self.memory = MemoryService(self.store, code_scope)
        self.tasks = TaskService(self.store)
        self.project_index = ProjectIndex(self.store, code_scope)
        self.maintenance = Maintenance(self.store)
        self.maintenance.register(
            "project_index", lambda options: self.project_index.refresh(**options)
        )
        self.observations = ObservationRegistry()
        self.context_budgeter.runtime = self
        self.research = ResearchController(self.store)
        self.development = DevelopmentPolicy()
        workspace = Path(
            getattr(self.store.record, "workspace") or self.access.workspace
        ).resolve()
        # Skills 可执行缝：内核工具 use_skill 经此取回 SKILL.md 正文。
        self.skills = SkillRegistry(
            workspace,
            self.registry,
            project_root=ApplicationPaths(self.store.root).project(
                self.store.record.project_id
            ),
            user=self.store.root / "skills",
        )
        # MCP 桥接缝：读取应用目录 projects/<project_id>/mcp.json 并动态注册 category="mcp" 工具。
        self.mcp = McpBridge(
            ApplicationPaths(self.store.root).project(self.store.record.project_id),
            self.registry,
            self.loader,
        ).configure(self.store.record.mcp_tools)
        self.mcp.image_handler = self._mcp_image
        self.mcp.image_config = lambda: self.images.config
        register_installed_extensions(self)

    def prepare_turn(self):
        self.maintenance.enqueue("project_index")
        self.maintenance.run_pending(limit=2)

    def deliver_observations(self, observations, *, call_id):
        self.tasks.observe(observations, call_id=call_id)
        changed = [
            item["path"] for item in observations if item["kind"] == "resource_changed"
        ]
        if changed:
            self.maintenance.enqueue("project_index", {"changed_paths": changed})
            self.maintenance.run_pending(limit=2)

    def register_maintenance(self, name, handler):
        self.maintenance.register(name, handler)

    def register_tool(self, descriptor, implementation):
        descriptor.spec.validate()
        if descriptor.name in self.registry:
            raise ValueError("Tool already registered: " + descriptor.name)
        self.loader.register(descriptor.name, implementation)
        self.registry.register(descriptor)

    def register_context(self, name, provider, *, priority=50):
        self.context_budgeter.providers.register(name, provider, priority=priority)

    def register_prompt(self, name, path, *, protocol=None, selector=None):
        if selector is not None and (
            not protocol or not callable(selector) or protocol in self.prompt.selectors
        ):
            raise ValueError("Prompt selector requires a unique protocol and callable")
        self.composer.register(name, Path(path), protocol=protocol)
        if selector is not None:
            self.prompt.register(protocol, selector)

    def register_verifier(self, name, planner):
        self.development.register_verifier(name, planner)

    def register_parser(self, suffix, parser):
        self.project_index.register_parser(suffix, parser)

    def _mcp_image(self, block):
        from genesisai.shared.security import ToolError

        if not getattr(self, "supports_images", False):
            raise ToolError(
                "image_unsupported", "当前模型尚未确认支持图片，请使用 /model 切换"
            )
        try:
            return self.images.from_mcp(block)
        except ValueError as exc:
            raise ToolError("invalid_image", str(exc)) from exc

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
        return self.catalog.definitions(
            history_lookup=self.store.record.run_runtime.get("history_lookup", False)
        )

    def execute(self, call: dict, approved: str | None = None) -> dict:
        invocation_id = uid("invocation")
        info = call if isinstance(call, dict) else {}
        prior = (
            self.store.record.calls.get(info.get("id", ""))
            if isinstance(info.get("id", ""), str)
            else None
        )
        details = {
            "invocation_id": invocation_id,
            "permission_mode": self.permission_mode,
            "approved": approved is not None,
            "prior_state": (prior or {}).get("state"),
        }
        self.logger.log_tool_execution_start(
            info.get("id"), info.get("name"), info.get("arguments"), **details
        )
        started = time.monotonic()
        try:
            result = self._execute(call, approved)
        except BaseException as exc:
            self.logger.error("tool_exception", exc, call=call, **details)
            raise
        self.logger.log_tool_execution_end(
            info.get("id"),
            info.get("name"),
            result,
            round((time.monotonic() - started) * 1000),
            **details,
        )
        return result

    def _execute(self, call: dict, approved: str | None = None) -> dict:
        self.catalog.refresh()
        # 先校验调用资格，再查询研究缓存或权限。
        try:
            self.executor._validate_call(call)
            prior = self.ledger.prior(call)
            if prior and prior.get("state") in {
                "succeeded",
                "failed",
                "started",
                "unknown",
            }:
                return self._validate_result(
                    call, self.executor.execute(call, approved)
                )
            descriptor = self.catalog.descriptor_for_execution(call["name"])
            Tool(
                name=descriptor.name,
                description=descriptor.description,
                parameters=descriptor.spec.parameters,
            )._validate_args(json.loads(call["arguments"]))
        except Exception:
            return self._validate_result(call, self.executor.execute(call, approved))
        blocked = self.research.before_execute(call)
        if blocked is not None:
            result = self._validate_result(call, blocked)
            self.ledger.finish(call, result, 0)
            return self.research.after_execute(call, result)
        result = self.executor.execute(call, approved)
        return self.research.after_execute(call, self._validate_result(call, result))

    def _validate_result(self, call, result):
        if isinstance(result, dict):
            if result.get("pending") is True and isinstance(
                result.get("preview"), dict
            ):
                return result
            error = result.get("error")
            if (
                result.get("ok") is True
                and error is None
                and isinstance(result.get("data"), dict)
            ):
                valid = True
                images = result["data"].get("images")
                if images is not None:
                    try:
                        validate_content(images)
                        valid = isinstance(images, list) and all(
                            p["type"] == "image" for p in images
                        )
                        valid = valid and all(
                            self.images.part(p["image_ref"]) == p for p in images
                        )
                    except ValueError:
                        valid = False
            else:
                valid = (
                    result.get("ok") is False
                    and isinstance(error, dict)
                    and isinstance(error.get("code"), str)
                    and isinstance(error.get("message"), str)
                )
            if valid:
                normalized = {
                    "call_id": call.get("id", ""),
                    "data": None,
                    "error": None,
                    "source_refs": [],
                    "artifact_refs": [],
                    "truncated": False,
                    **result,
                }
                if all(
                    isinstance(normalized[key], list)
                    and all(isinstance(ref, str) for ref in normalized[key])
                    for key in ("source_refs", "artifact_refs")
                ):
                    return normalized
        return error_result(call, "invalid_result", "工具返回了无效的结果结构")

    def reject(self, call: dict) -> dict:
        result = self.executor.reject(call)
        self.logger.record("tool_rejected", call=call, result=result)
        return result

    @property
    def permission_mode(self) -> str:
        return self.policy.mode

    def set_permission(self, mode: str) -> None:
        policy = PermissionPolicy(mode)
        previous = getattr(self.store.record, "permission_mode", "user")
        if previous != mode:
            self.store.record.permission_mode = mode
            try:
                self.store.commit()
            except Exception:
                self.store.record.permission_mode = previous
                raise
            if mode == "user" and hasattr(self, "mcp"):
                self.mcp.close()
        self.policy = policy
        self.access.host_access = mode == "auto"
        if hasattr(self, "executor"):
            self.executor.policy = policy

    def confirmation_message(self, name: str) -> str:
        """根据工具固定 Spec 返回准确的待确认类型。"""
        self.registry.refresh()
        if name in self.registry:
            permission = self.registry.get(name).spec.permission
            if self.registry.get(name).category == "mcp":
                return "需要确认 MCP 服务启动或调用"
            if permission == "network":
                return "需要确认公开网络请求"
            if permission == "write":
                return "需要确认输出文件写入"
            if permission == "shell":
                return "需要确认受控命令执行"
        return "需要确认当前操作"

    def inventory(self) -> dict[str, list[dict]]:
        """返回启用/禁用工具清单（CLI 展示用）。"""
        self.catalog.refresh()
        return self.catalog.inventory()

    @staticmethod
    def error(call: dict, code: str, message: str, retryable: bool = False) -> dict:
        return error_result(call, code, message, retryable)
