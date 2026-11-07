"""组装 Tool Runtime，并为 Runner 与 CLI 提供单一入口。"""

from __future__ import annotations

from copy import copy
from dataclasses import replace
import json
import time
from pathlib import Path
from genesisai.shared.ids import uid

from genesisai.core.extensions.lsp.extension import register_lsp
from genesisai.core.jobs.service import JobService
from genesisai.core.tools.base import ToolContext
from genesisai.core.memory.scope import CodeScope
from genesisai.core.tools.catalog import ToolCatalog
from genesisai.core.tools.selection import allowed_by_constraints, discover, select_tools
from genesisai.core.tools.output import project_output
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
from genesisai.core.capabilities.service import CapabilityService
from genesisai.core.capabilities.tools import register_lifecycle
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
            hosted_network=network is not None,
            hosted_providers=providers is not None or network is not None,
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
        self.maintenance = Maintenance(self.store, limits=lambda: self.jobs._limits())
        self.maintenance.register(
            "project_index", lambda options: self.project_index.refresh(**options, progress=self._maintenance_progress)
        )
        self.maintenance.register('rebuild', self._rebuild_memory)
        self.observations = ObservationRegistry()
        self.context_budgeter.runtime = self
        self.research = ResearchController(self.store)
        self.development = DevelopmentPolicy()
        workspace = Path(
            getattr(self.store.record, "workspace") or self.access.workspace
        ).resolve()
        # Skills 可执行缝：内核工具 use_skill 经此取回 SKILL.md 正文。
        self.skills = SkillRegistry(self.registry)
        self.lsp_error = None
        self.lsp = None
        try:
            self.lsp = register_lsp(ApplicationPaths(store.root).project(store.record.project_id), self.registry, self.loader, state_root=store.root)
        except ValueError as exc:
            self.lsp_error = str(exc)
            self.logger.error("lsp_config_error", exc)
        self.jobs = JobService(store, self.logger.journal.clean, logger=self.logger)
        self.mcp = McpBridge(
            ApplicationPaths(self.store.root).project(self.store.record.project_id),
            self.registry,
            self.loader,
        ).configure(self.store.record.mcp_tools, servers=[])
        self.mcp.image_handler = self._mcp_image
        self.mcp.image_config = lambda: self.images.config
        register_installed_extensions(self)
        self.capabilities = CapabilityService(self)
        self._capability_tools = set()
        self._capability_signature = None
        self.capability_diagnostics = []
        self.tool_allowlist = None
        register_lifecycle(self)
        self.refresh_capabilities()
        if self.maintenance._queued():
            self.maintenance.start()

    def bind_model(self, model):
        self.capabilities.model = model

    def collect_tool_job(self, job_id, call_id):
        from genesisai.core.jobs.tool_operation import collect_tool
        return collect_tool(self, job_id, call_id)

    def refresh_capabilities(self):
        from genesisai.core.capabilities.activation import refresh
        refresh(self)

    def prepare_turn(self):
        self.maintenance.enqueue("project_index")

    def deliver_observations(self, observations, *, call_id):
        self.tasks.observe(observations, call_id=call_id)
        changed = [
            item["path"] for item in observations if item["kind"] == "resource_changed"
        ]
        if changed:
            self.project_index.mark_changed(changed)
            self.maintenance.enqueue("project_index", {"changed_paths": changed})

    def register_maintenance(self, name, handler):
        self.maintenance.register(name, handler)
        self.maintenance.start()

    @staticmethod
    def _maintenance_progress(value):
        from genesisai.shared.run_control import current_run
        if control := current_run.get():
            control.emit('maintenance_progress', value)

    def _rebuild_memory(self, options):
        if options:
            raise ValueError('完整记忆重建不接受额外参数')
        return {'project': self.project_index.refresh(force=True, progress=self._maintenance_progress),
                'search': self.memory.rebuild_search()}

    def register_tool(self, descriptor, implementation):
        descriptor.spec.validate()
        if descriptor.name in self.registry:
            raise ValueError("Tool already registered: " + descriptor.name)
        descriptor = replace(descriptor, source="external", scope="user")
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
            self.context.hosted_network = True

    @property
    def providers(self) -> list:
        return self._providers

    @providers.setter
    def providers(self, value: list) -> None:
        self._providers = value
        if hasattr(self, "context"):
            self.context.providers = value
            self.context.hosted_providers = True

    def completion_issue(self):
        """Check host-owned acceptance facts before delivering a completed task."""
        state = self.store.record.run_runtime
        if state.get("pending_execution_jobs"):
            return "仍有未收取或结果未知的作业，请用 read_job 等待并核对：" + ", ".join(state["pending_execution_jobs"])
        if state.get("constraints", {}).get("read_only") or not state.get("execution_requested"):
            return None
        issue = self.tasks.completion_issue()
        if issue:
            return issue
        if "capability_builder" in state.get("protocols", []):
            target = state.get("capability_target")
            if not target:
                return "能力构建尚无实际草稿。请执行已授权的构建步骤；计划或下一步说明不是完成结果。"
            item = self.capabilities.store.get(target["id"])
            actual = self.capabilities.summary(item)["state"]
            required = state.get("capability_goal", "registered")
            ranks = {"draft": 0, "tested": 1, "registered": 2}
            if ranks[actual] < ranks[required]:
                return f"能力实际状态为{actual}，验收目标为{required}。继续修订/测试/请求用户验收注册，不得宣称已安装。"
        elif not self.tasks.has_execution_evidence() and not any(item.get("ok") and item.get("tool") not in {"plan", "discover_tools", "search_skills", "use_skill", "inspect_capabilities"}
                     and not (item.get("tool") in self.registry and self.registry.get(item["tool"]).spec.completion_field)
                     for item in state.get("tool_activity", [])):
            return "实际执行任务尚无工具事实。请先检查或执行；若无需修改，应取得当前项目状态支持该结论。"
        return None

    def definitions(self, *, names=None) -> list[dict]:
        self.refresh_capabilities()
        self.catalog.refresh()
        available = self.catalog.available_tools()
        if self.tool_allowlist is not None:
            available = [item for item in available if item.name in self.tool_allowlist]
        if names is not None:
            return [item.definition() for item in available if item.name in names]
        skill_tools = self.skills.active_tools(self.store.record.tool_runtime['active_skills'], available=self.catalog.available)
        state = {**self.store.record.run_runtime, "pending_tools": self.store.record.pending}
        selected = select_tools(available, state, skill_tools,
                                token_budget=max(1, self.context_budgeter.config.input_limit // 8))
        return [item.definition() for item in selected]

    def submission_batch(self, calls):
        from genesisai.core.jobs.submission import submission_batch
        return submission_batch(self, calls)

    def discover_tools(self, query='', limit=20, activate=True, *, offset=0, provider=None, category=None, source=None):
        self.catalog.refresh()
        matches = discover([item for item in self.catalog.available_tools()
                            if (self.tool_allowlist is None or item.name in self.tool_allowlist)
                            and allowed_by_constraints(item, self.store.record.run_runtime)], query, limit + 1,
                            offset=offset, provider=provider, category=category, source=source)
        more = len(matches) > limit
        matches = matches[:limit]
        state = self.store.record.run_runtime
        if activate:
            selected = [*state.get("discovered_tools", []), *(item["name"] for item in matches)]
            state["discovered_tools"] = list(dict.fromkeys(reversed(selected)))[::-1]
            state["explicit_discovery"] = next((item["name"] for item in matches if item["name"].casefold() == query.strip().casefold()), None)
        selected_names = {item["function"]["name"] for item in self.definitions()} if activate else set()
        return {"available_next_round": [item for item in matches if item["name"] in selected_names], "candidates": matches,
                "next_offset": offset + limit if more else None,
                "deferred": [item["name"] for item in matches if activate and item["name"] not in selected_names]}

    def model_content(self, call, result):
        descriptor = self.registry.get(call["name"]) if call["name"] in self.registry else None
        limit = descriptor.spec.max_output_chars if descriptor else 12000
        return project_output(self.store, call, result, limit)

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
        self.refresh_capabilities()
        if self.tool_allowlist is not None and call.get("name") not in self.tool_allowlist:
            return error_result(call, "tool_unavailable", "此试运行只允许 Skill 声明的工具")
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
            descriptor.validate_arguments(json.loads(call["arguments"]))
        except Exception:
            return self._validate_result(call, self.executor.execute(call, approved))
        if not allowed_by_constraints(descriptor, self.store.record.run_runtime):
            result = error_result(call, "task_constraint", "当前用户任务约束不允许此操作；调用未执行。请使用允许的能力继续。")
            self.ledger.finish(call, result, 0)
            return result
        if descriptor.spec.side_effect and descriptor.spec.path_scope == "workspace" and self.store.record.status == "running":
            from genesisai.core.extensions.rules import RulesLoader
            from genesisai.shared.security import digest
            args = json.loads(call["arguments"])
            paths = []
            def collect(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {"path", "cwd", "source", "destination", "new_path"} and isinstance(child, str):
                            paths.append(child)
                        elif isinstance(child, (dict, list)):
                            collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(args)
            blocks = RulesLoader(self.store.workspace, application_root=self.store.root).load(paths)
            state = self.store.record.run_runtime
            changed = [b for b in blocks if state.get("presented_rules", {}).get(b["path"]) != digest(b)]
            if changed:
                state["rule_paths"] = list(dict.fromkeys([*state.get("rule_paths", []), *paths]))
                result = error_result(call, "rules_context_changed", "操作范围存在尚未呈现或已变更的 AGENTS.md；已加入下一轮上下文，请读取适用规则后继续此操作。")
                self.ledger.finish(call, result, 0)
                return result
        blocked = self.research.before_execute(call)
        if blocked is not None:
            result = self._validate_result(call, blocked)
            self.ledger.finish(call, result, 0)
            return self.research.after_execute(call, result)
        result = self.executor.execute(call, approved)
        result = self._validate_result(call, result)
        if "input_request" in result:
            return result
        return self.research.after_execute(call, result)

    def _validate_result(self, call, result):
        if isinstance(result, dict):
            question = result.get("input_request")
            if (isinstance(question, dict) and isinstance(question.get("question"), str)
                    and isinstance(question.get("options"), list)
                    and call.get("name") in self.registry and self.registry.get(call["name"]).spec.awaits_input):
                return result
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

    def cancel_question(self, call, result):
        self.ledger.finish(call, result, 0)

    def reject(self, call: dict) -> dict:
        result = self.executor.reject(call)
        self.logger.record("tool_rejected", call=call, result=result)
        return result

    @property
    def permission_mode(self) -> str:
        return self.policy.mode

    def set_permission(self, mode: str) -> None:
        from genesisai.core.tools.authorization import AuthorizationService
        if not hasattr(self, 'authorizations'):
            self.authorizations = AuthorizationService(self.store)
        policy = PermissionPolicy(mode, self.authorizations)
        previous = getattr(self.store.record, "permission_mode", "user")
        if previous != mode:
            self.store.record.permission_mode = mode
            try:
                self.store.commit()
            except Exception:
                self.store.record.permission_mode = previous
                raise
            if mode == "user" and hasattr(self, "mcp"):
                self.jobs.stop_all()
                self.close()
        self.policy = policy
        self.access.host_access = mode == "auto"
        if hasattr(self, "executor"):
            self.executor.policy = policy

    def close(self):
        self.maintenance.close()
        if self.lsp:
            self.lsp.close()
        self.jobs.close()
        self.mcp.close()

    def cancel_jobs(self):
        self.jobs.stop_run(self.store.record.run_id)

    def confirmation_message(self, name: str) -> str:
        """根据工具固定 Spec 返回准确的待确认类型。"""
        self.registry.refresh()
        if name in self.registry:
            spec = self.registry.get(name).spec
            if spec.launches_service:
                return "需要确认启动外部服务执行查询"
            permission = spec.permission
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
        """返回内置/外挂清单；每项包含是否可用。"""
        self.refresh_capabilities()
        self.catalog.refresh()
        inventory = self.catalog.inventory()
        if self.lsp_error:
            inventory["external"].append({"name": "lsp_query", "category": "lsp", "source": "external", "enabled": False,
                                          "description": "语言服务查询", "reason": self.lsp_error})
        return inventory

    @staticmethod
    def error(call: dict, code: str, message: str, retryable: bool = False) -> dict:
        return error_result(call, code, message, retryable)
