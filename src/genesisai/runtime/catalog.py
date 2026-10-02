"""工具目录查询和 Core、Active、Available、Disabled 状态。"""

from __future__ import annotations

from genesisai.shared.security import ToolError
from genesisai.runtime.registry import CORE_NAMES, ToolRegistry


TOOL_DEPENDENCIES = {"search_fetch": ("search_query",)}


class ToolCatalog:
    """工具目录查询和 Core、Active、Available、Disabled 四层状态管理。"""
    def __init__(self, registry: ToolRegistry, loader, store, active_limit: int = 8):
        self.registry = registry
        self.loader = loader
        self.store = store
        self.active_limit = active_limit
        self._restore_active()

    def _disabled_reason(self, descriptor) -> str | None:
        if not descriptor.enabled:
            return "已在 tool.yaml 中禁用"
        return descriptor.spec.unavailable_reason()

    def _restore_active(self) -> None:
        saved = self.store.data["tool_runtime"]["active_tools"]
        normalized = self._with_dependencies(saved)
        if normalized != saved:
            self.store.data["tool_runtime"]["active_tools"] = normalized
            saved = normalized
            self.store.save()
        for name in saved:
            if name in self.registry:
                descriptor = self.registry.get(name)
                if descriptor.category != "core" and not self._disabled_reason(descriptor):
                    try:
                        self.loader.load(descriptor)
                    except ToolError:
                        self.store.event("tool", tool=name, ok=False, code="tool_unavailable")
                    continue
            self.store.event("tool", tool=name, ok=False, code="tool_unavailable")

    def refresh(self) -> None:
        self.registry.refresh()

    @property
    def active_names(self) -> list[str]:
        return list(self.store.data["tool_runtime"]["active_tools"])

    def status(self, name: str) -> tuple[str, str | None]:
        if name not in self.registry:
            raise ToolError("unknown_tool", "未知工具")
        descriptor = self.registry.get(name)
        reason = self._disabled_reason(descriptor)
        if reason:
            return "disabled", reason
        if descriptor.category == "core":
            return "core", None
        if name in self.active_names:
            return "active", None
        return "available", None

    def descriptor_for_execution(self, name: str):
        if name not in self.registry:
            if name in self.active_names:
                raise ToolError("tool_unavailable", "已加载工具当前不存在")
            raise ToolError("unknown_tool", "未知工具")
        descriptor = self.registry.get(name)
        status, reason = self.status(name)
        if status == "disabled":
            raise ToolError("tool_unavailable", f"工具不可用：{reason}")
        if status not in {"core", "active"}:
            raise ToolError("tool_unavailable", "工具尚未加载，请先使用 tool_load")
        return descriptor

    def definitions(self) -> list[dict]:
        names = [*CORE_NAMES]
        names.extend(
            name for name in self.active_names
            if name in self.registry and self.status(name)[0] == "active"
        )
        return [self.registry.get(name).definition() for name in names]

    def search(self, query: str, category: str | None = None, include_disabled: bool = False, limit: int = 10) -> list[dict]:
        needle = query.strip().casefold()
        results = []
        for descriptor in self.registry.values():
            status, reason = self.status(descriptor.name)
            if category and descriptor.category != category:
                continue
            if status == "disabled" and not include_disabled:
                continue
            aliases = {
                "search_query": "web network search query internet news 搜索 网络 网页 新闻 查询",
                "search_fetch": "web network fetch page internet news 抓取 网页 正文 新闻 来源",
                "file_list": "filesystem local files directory 文件 本地 目录 列表",
                "file_search": "filesystem local files search 文件 本地 内容 搜索",
                "file_read": "filesystem local files read 文件 本地 读取 正文 资料",
                "file_create": "filesystem local files write 文件 本地 写入 报告",
                "file_copy": "filesystem local files copy 文件 本地 复制 原件",
                "file_patch": "filesystem code patch edit modify 文件 代码 补丁 修改",
                "file_move": "filesystem move rename 文件 移动 重命名",
                "file_delete": "filesystem delete remove 文件 删除",
                "directory_create": "filesystem directory folder create 目录 创建",
                "shell_run": "development shell command build execute 命令 构建 执行",
                "test_run": "development test pytest unittest npm cargo 测试",
                "python_project": "python pyproject venv pytest unittest package source tests 项目 环境 测试框架",
                "project_plan": "project planning acceptance agent_docs 策划 计划 验收 开发任务",
                "git_status": "git status repository 工作区 状态",
                "git_diff": "git diff changes 变更 差异",
                "git_log": "git log history 历史 提交记录",
                "git_show": "git show commit file 提交 查看",
                "git_branch_current": "git branch current 分支 当前",
                "memory_search": "memory search catalog 记忆 检索 目录",
                "memory_read": "memory read content 记忆 读取 正文",
                "memory_write": "memory remember write 记忆 记住 写入",
                "memory_forget": "memory forget delete 记忆 忘记 删除",
                "memory_validate": "memory validate source hash 记忆 验证 来源",
            }.get(descriptor.name, "")
            haystack = " ".join((descriptor.name, descriptor.category, descriptor.description, aliases)).casefold()
            terms = [part for part in needle.split() if part]
            cjk_pairs = {
                part[index:index + 2]
                for part in terms
                if any('\u4e00' <= char <= '\u9fff' for char in part)
                for index in range(max(0, len(part) - 1))
            }
            probes = [*terms, *sorted(cjk_pairs)]
            if needle and not (needle in haystack or any(probe in haystack for probe in probes)):
                continue
            results.append({
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
                "status": status,
                **({"reason": reason} if reason else {}),
            })
        return results[:limit]

    def describe(self, name: str) -> dict:
        if name not in self.registry:
            raise ToolError("unknown_tool", "未知工具")
        descriptor = self.registry.get(name)
        status, reason = self.status(name)
        spec = descriptor.spec
        return {
            "name": name,
            "category": descriptor.category,
            "description": descriptor.description,
            "status": status,
            "parameters": spec.parameters,
            "permission": spec.permission,
            "path_scope": spec.path_scope,
            "network": spec.network,
            "side_effect": spec.side_effect,
            "timeout_seconds": spec.timeout_seconds,
            "max_output_chars": spec.max_output_chars,
            **({"reason": reason} if reason else {}),
        }

    def activate(self, names: list[str], *, replace: bool = False) -> list[str]:
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            raise ToolError("validation_error", "names 必须是非空工具名数组")
        requested = self._with_dependencies(list(dict.fromkeys(names)))
        descriptors = []
        additions = []
        for name in requested:
            if name not in self.registry:
                raise ToolError("unknown_tool", f"未知工具：{name}")
            descriptor = self.registry.get(name)
            status, reason = self.status(name)
            if status == "disabled":
                raise ToolError("tool_unavailable", f"工具不可用：{name}（{reason}）")
            if status == "core":
                continue
            descriptors.append(descriptor)
            if replace or status != "active":
                additions.append(name)
        existing = [] if replace else [
            name for name in self.active_names
            if name in self.registry and self.status(name)[0] == "active"
        ]
        candidate = [*existing, *additions]
        if len(candidate) > self.active_limit:
            raise ToolError("active_limit", f"Active Tools 不能超过 {self.active_limit} 个")
        loader_checkpoint = self.loader.loaded_names()
        self.loader.load_many(descriptors)
        previous = self.active_names
        self.store.data["tool_runtime"]["active_tools"] = candidate
        try:
            self.store.save()
        except Exception:
            self.store.data["tool_runtime"]["active_tools"] = previous
            self.loader.discard_newer_than(loader_checkpoint)
            raise
        return self.active_names

    @staticmethod
    def _with_dependencies(names: list[str]) -> list[str]:
        expanded: list[str] = []
        for name in names:
            for dependency in TOOL_DEPENDENCIES.get(name, ()):
                if dependency not in expanded:
                    expanded.append(dependency)
            if name not in expanded:
                expanded.append(name)
        return expanded

    def reset(self) -> None:
        if self.active_names:
            self.store.data["tool_runtime"]["active_tools"] = []
            self.store.save()

    def snapshot(self) -> dict[str, list[dict]]:
        groups = {"core": [], "active": [], "available": [], "disabled": []}
        for descriptor in self.registry.values():
            status, reason = self.status(descriptor.name)
            groups[status].append({
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
                **({"reason": reason} if reason else {}),
            })
        for name in self.active_names:
            if name not in self.registry:
                groups["disabled"].append({
                    "name": name,
                    "category": "unknown",
                    "description": "会话中保存的工具已不存在",
                    "reason": "工具当前不存在",
                })
        return groups

