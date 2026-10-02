"""确定性的、人类可读的项目工作记忆，存储在 agent_docs 中。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from genesisai.state.store import atomic_json, uid


MANAGED_BY = "GenesisAI"
INDEX_FIELDS = frozenset({"schema_version", "managed_by", "project", "active_task", "tasks", "files"})
TASK_FIELDS = frozenset({
    "id", "title", "status", "created_at", "updated_at", "run_ids", "plan_ready",
    "last_write_at", "last_verification_at", "verification_passed", "observed_files", "drifted_files",
})
TASK_STATUSES = frozenset({
    "planning", "planned", "in_progress", "blocked", "needs_verification", "verified", "cancelled",
})
SECRET = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|credential)\s*[:=]\s*([^\s`]+)"
)


class AgentDocsError(ValueError):
    """托管的项目工作记忆缺失、冲突或格式错误。"""


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _clean(value: str, limit: int = 24000) -> str:
    text = SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(value)).strip()
    if len(text) > limit:
        raise AgentDocsError(f"agent_docs 内容超过 {limit} 字符")
    return text


class AgentDocsManager:
    """管理和更新恢复项目所需的最小文档集。"""

    def __init__(self, workspace: Path, *, create: bool = False):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "agent_docs"
        self.index_path = self.root / "index.json"
        if create:
            self.initialize()
        self.data = self._load() if self.index_path.is_file() else None

    @property
    def available(self) -> bool:
        return self.data is not None

    @classmethod
    def exists(cls, workspace: Path) -> bool:
        return (Path(workspace).resolve() / "agent_docs" / "index.json").is_file()

    def initialize(self) -> dict:
        if self.root.exists() and not self.index_path.is_file():
            raise AgentDocsError("agent_docs 已存在但没有 GenesisAI 所有权标记，已停止接管")
        if self.index_path.is_file():
            self.data = self._load()
            return self.summary()
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "tasks").mkdir()
        now = time.time()
        self.data = {
            "schema_version": 1,
            "managed_by": MANAGED_BY,
            "project": {"workspace": str(self.workspace), "created_at": now, "updated_at": now},
            "active_task": None,
            "tasks": [],
            "files": {},
        }
        self._save_index()
        self._write("README.md", self._readme())
        self._write("project.md", self._project_overview())
        self._write("architecture.md", "# 架构\n\n尚未记录经过验证的架构结论。\n")
        self._write("decisions.md", "# 技术决策\n\n尚无技术决策。\n")
        self._write("commands.md", "# 已验证命令\n\n只记录真实执行成功的命令。\n")
        return self.summary()

    def _load(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentDocsError("agent_docs/index.json 损坏，现有文档未被覆盖") from exc
        self._validate(data)
        if Path(data["project"]["workspace"]).resolve() != self.workspace:
            raise AgentDocsError("agent_docs 属于另一个工作区，已停止加载")
        return data

    @staticmethod
    def _validate(data: object) -> None:
        if not isinstance(data, dict) or set(data) != INDEX_FIELDS:
            raise AgentDocsError("agent_docs 索引字段无效")
        if data.get("schema_version") != 1 or data.get("managed_by") != MANAGED_BY:
            raise AgentDocsError("agent_docs 所有权或版本无效")
        if not isinstance(data.get("project"), dict) or set(data["project"]) != {"workspace", "created_at", "updated_at"}:
            raise AgentDocsError("agent_docs project 字段无效")
        if not isinstance(data.get("tasks"), list) or not isinstance(data.get("files"), dict):
            raise AgentDocsError("agent_docs tasks/files 字段无效")
        identifiers = set()
        for item in data["tasks"]:
            if not isinstance(item, dict) or set(item) != TASK_FIELDS:
                raise AgentDocsError("agent_docs task 字段无效")
            if item["id"] in identifiers or item["status"] not in TASK_STATUSES:
                raise AgentDocsError("agent_docs task ID 或状态无效")
            identifiers.add(item["id"])
            if not isinstance(item["run_ids"], list) or not isinstance(item["observed_files"], dict) or not isinstance(item["drifted_files"], list):
                raise AgentDocsError("agent_docs task 运行记录无效")
        if data["active_task"] is not None and data["active_task"] not in identifiers:
            raise AgentDocsError("agent_docs active_task 无效")
        for relative, digest in data["files"].items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or not isinstance(digest, str):
                raise AgentDocsError("agent_docs 文件索引越界")

    def _save_index(self) -> None:
        self.data["project"]["updated_at"] = time.time()
        self._validate(self.data)
        atomic_json(self.index_path, self.data)

    def _write(self, relative: str, content: str, *, force: bool = False) -> None:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() != ".md":
            raise AgentDocsError("agent_docs 只管理目录内 Markdown 文件")
        known = self.data["files"].get(Path(relative).as_posix())
        if path.is_file() and known and _file_sha(path) != known and not force:
            raise AgentDocsError(f"agent_docs/{relative} 已被外部修改，已停止覆盖")
        if path.exists() and not path.is_file():
            raise AgentDocsError(f"agent_docs/{relative} 不是普通文件")
        payload = (_clean(content).rstrip() + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self.data["files"][Path(relative).as_posix()] = _sha_bytes(payload)
        self._save_index()

    def _append(self, relative: str, heading: str, content: str) -> None:
        path = self.root / relative
        previous = path.read_text(encoding="utf-8") if path.is_file() else f"# {heading}\n"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self._write(relative, previous.rstrip() + f"\n\n## {stamp}\n\n{_clean(content)}\n")

    def _readme(self) -> str:
        return (
            "# GenesisAI 项目工作记忆\n\n"
            "本目录由 GenesisAI 管理，用于保存项目策划、实施计划、进度、技术决策、真实验收结果和交接信息。\n\n"
            "- 代码、配置、测试和 Git 状态始终是最高事实来源。\n"
            "- 不在这里复制完整源码、原始命令日志或任何密钥。\n"
            "- 如需人工修改，请先结束 GenesisAI 会话；外部改动会触发冲突保护。\n"
            "- `.genesis/` 保存运行状态，`agent_docs/` 保存可审查的项目工作记忆。\n"
        )

    def _project_overview(self) -> str:
        markers = [name for name in ("pyproject.toml", "setup.py", "requirements.txt", "pytest.ini", ".git") if (self.workspace / name).exists()]
        return (
            "# 项目概览\n\n"
            f"- 工作区：`{self.workspace}`\n"
            f"- 已识别入口：{', '.join(f'`{item}`' for item in markers) if markers else '尚未识别'}\n"
            "- 项目目标：等待首个开发任务补充。\n"
            "- 事实优先级：源码与测试 > agent_docs > 长期 Memory > 当前模型推测。\n"
        )

    def begin_task(self, objective: str, *, run_id: str | None = None) -> dict:
        self._require_available()
        active = self.active_task()
        if active and active["status"] not in {"verified", "cancelled"}:
            if run_id and run_id not in active["run_ids"]:
                active["run_ids"].append(run_id)
            active["updated_at"] = time.time()
            self.reconcile()
            self._save_index()
            return dict(active)
        identifier = uid("task")
        now = time.time()
        task = {
            "id": identifier,
            "title": _clean(objective, 200),
            "status": "planning",
            "created_at": now,
            "updated_at": now,
            "run_ids": [run_id] if run_id else [],
            "plan_ready": False,
            "last_write_at": None,
            "last_verification_at": None,
            "verification_passed": False,
            "observed_files": {},
            "drifted_files": [],
        }
        self.data["tasks"].append(task)
        self.data["active_task"] = identifier
        base = f"tasks/{identifier}"
        self._write(f"{base}/brief.md", f"# 任务说明\n\n## 目标\n\n{task['title']}\n\n## 非目标\n\n待策划阶段确认。\n")
        self._write(
            f"{base}/plan.md",
            "# 实施计划\n\n"
            "当前为初始循环，模型需在修改产品代码前补充具体步骤。\n\n"
            "- [ ] 检查项目入口和现状\n"
            "- [ ] 明确实现步骤和验收条件\n"
            "- [ ] 分步实施并检查差异\n"
            "- [ ] 运行针对性测试和总体验收\n",
        )
        self._write(f"{base}/progress.md", "# 执行进度\n\n任务已创建，等待形成具体计划。\n")
        self._write(f"{base}/acceptance.md", "# 验收记录\n\n尚未执行验收。\n")
        self._write(f"{base}/handoff.md", "# 交接信息\n\n当前处于策划阶段。\n")
        self._save_index()
        return dict(task)

    def set_plan(self, *, steps: list[str], acceptance: list[str], non_goals: list[str] | None = None) -> dict:
        task = self._active_required()
        clean_steps = [_clean(item, 500) for item in steps if str(item).strip()]
        clean_acceptance = [_clean(item, 500) for item in acceptance if str(item).strip()]
        clean_non_goals = [_clean(item, 500) for item in (non_goals or []) if str(item).strip()]
        if not clean_steps or not clean_acceptance:
            raise AgentDocsError("项目计划至少需要一个步骤和一个验收条件")
        base = f"tasks/{task['id']}"
        body = "# 实施计划\n\n## 步骤\n\n" + "\n".join(f"- [ ] {item}" for item in clean_steps)
        body += "\n\n## 验收条件\n\n" + "\n".join(f"- [ ] {item}" for item in clean_acceptance)
        body += "\n\n## 非目标\n\n" + ("\n".join(f"- {item}" for item in clean_non_goals) if clean_non_goals else "- 未声明")
        self._write(f"{base}/plan.md", body)
        task["plan_ready"] = True
        task["status"] = "planned"
        task["updated_at"] = time.time()
        self._save_index()
        return dict(task)

    def record_tool(self, name: str, result: dict, args: dict | None = None) -> None:
        task = self.active_task()
        if not task:
            return
        now = time.time()
        data = result.get("data") if isinstance(result, dict) else None
        data = data if isinstance(data, dict) else {}
        ok = bool(result.get("ok"))
        args = args or {}
        if name == "file_read" and ok and data.get("sha256"):
            self._observe(args.get("path"), data["sha256"])
        if name in {"file_patch", "file_create", "file_delete", "file_move"} and ok:
            task["status"] = "in_progress"
            task["last_write_at"] = now
            task["verification_passed"] = False
            path = data.get("path") or data.get("destination") or args.get("path") or args.get("destination")
            digest = data.get("after_sha256")
            if path and not digest:
                candidate = Path(path)
                candidate = candidate.resolve() if candidate.is_absolute() else (self.workspace / candidate).resolve()
                if candidate.is_file() and candidate.is_relative_to(self.workspace):
                    digest = _file_sha(candidate)
            if path and digest:
                self._observe(path, digest)
            self._append(f"tasks/{task['id']}/progress.md", "文件变更", f"- 工具：`{name}`\n- 结果：成功\n- 路径：`{path or '未提供'}`")
            validation = data.get("validation") if isinstance(data.get("validation"), dict) else None
            if validation and validation.get("passed") is True:
                task["last_verification_at"] = now
                task["verification_passed"] = True
                task["status"] = "verified"
                self._append(
                    f"tasks/{task['id']}/acceptance.md",
                    "静态验收",
                    f"- 框架：`{validation.get('framework', 'static')}`\n"
                    f"- 文件：`{validation.get('file', path or '未提供')}`\n"
                    "- 结果：通过",
                )
        elif name == "test_run" and ok:
            passed = bool(data.get("passed"))
            task["last_verification_at"] = now
            task["verification_passed"] = passed
            task["status"] = "verified" if passed and task.get("last_write_at") else ("in_progress" if passed else "blocked")
            command = data.get("command") or []
            summary = data.get("summary") or {}
            entry = (
                f"- 命令：`{' '.join(str(item) for item in command)}`\n"
                f"- 退出码：`{data.get('exit_code')}`\n"
                f"- 结果：{'通过' if passed else '失败'}\n"
                f"- 摘要：`{json.dumps(summary, ensure_ascii=False)}`"
            )
            self._append(f"tasks/{task['id']}/acceptance.md", "测试执行", entry)
            if passed:
                self._append("commands.md", "已验证命令", f"`{' '.join(str(item) for item in command)}`")
        elif not ok:
            error = result.get("error") or {}
            self._append(
                f"tasks/{task['id']}/progress.md", "工具失败",
                f"- 工具：`{name}`\n- 错误：`{error.get('code', 'unknown')}`\n- 说明：{error.get('message', '未提供')}",
            )
        task["updated_at"] = now
        self._save_index()

    def finish_run(self, status: str, answer: str = "") -> dict | None:
        task = self.active_task()
        if not task:
            return None
        if status == "cancelled":
            task["status"] = "cancelled"
        elif status == "completed":
            write_at = task.get("last_write_at")
            verified_at = task.get("last_verification_at")
            valid = bool(task.get("verification_passed") and verified_at and (not write_at or verified_at >= write_at))
            task["status"] = "verified" if valid else "needs_verification"
        elif status in {"failed", "partial", "limit_reached", "interrupted"}:
            if status == "failed":
                task["status"] = "blocked"
            elif task.get("last_write_at") and not task.get("verification_passed"):
                task["status"] = "needs_verification"
            else:
                task["status"] = "in_progress"
        task["updated_at"] = time.time()
        handoff = (
            f"# 交接信息\n\n- Runner 状态：`{status}`\n"
            f"- 任务状态：`{task['status']}`\n"
            f"- 最近验收：{'通过' if task['verification_passed'] else '未通过或未执行'}\n"
            f"- 漂移文件：{', '.join(task['drifted_files']) if task['drifted_files'] else '无'}\n\n"
            "## 最近交付摘要\n\n" + (_clean(answer, 3000) if answer else "尚无最终交付。")
        )
        self._write(f"tasks/{task['id']}/handoff.md", handoff)
        self._save_index()
        return dict(task)

    def reconcile(self) -> list[str]:
        task = self.active_task()
        if not task:
            return []
        drifted = []
        for relative, expected in task["observed_files"].items():
            path = (self.workspace / relative).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file() or _file_sha(path) != expected:
                drifted.append(relative)
        task["drifted_files"] = sorted(drifted)
        if drifted and task["status"] == "verified":
            task["status"] = "needs_verification"
            task["verification_passed"] = False
        task["updated_at"] = time.time()
        self._save_index()
        return task["drifted_files"]

    def context(self, *, limit: int = 7000) -> dict:
        self._require_available()
        self.reconcile()
        task = self.active_task()
        if not task:
            return {"available": True, "active_task": None}
        base = self.root / "tasks" / task["id"]
        documents = {}
        remaining = limit
        for name in ("brief.md", "plan.md", "progress.md", "acceptance.md", "handoff.md"):
            path = base / name
            if not path.is_file() or remaining <= 0:
                continue
            value = path.read_text(encoding="utf-8")[-remaining:]
            documents[name] = value
            remaining -= len(value)
        return {
            "available": True,
            "task": {key: task[key] for key in ("id", "title", "status", "plan_ready", "verification_passed", "drifted_files")},
            "documents": documents,
        }

    def summary(self) -> dict:
        self._require_available()
        task = self.active_task()
        return {
            "root": str(self.root),
            "managed_by": self.data["managed_by"],
            "tasks": len(self.data["tasks"]),
            "active_task": task["id"] if task else None,
            "active_status": task["status"] if task else None,
        }

    def active_task(self) -> dict | None:
        if not self.available or self.data["active_task"] is None:
            return None
        return next((item for item in self.data["tasks"] if item["id"] == self.data["active_task"]), None)

    def _active_required(self) -> dict:
        task = self.active_task()
        if not task:
            raise AgentDocsError("agent_docs 没有活动任务")
        return task

    def _require_available(self) -> None:
        if not self.available:
            raise AgentDocsError("工作区尚未初始化 agent_docs")

    def _observe(self, path_value, digest: str) -> None:
        if not path_value or not digest:
            return
        path = Path(path_value)
        path = path.resolve() if path.is_absolute() else (self.workspace / path).resolve()
        if path.is_relative_to(self.workspace) and not path.is_relative_to(self.root):
            self._active_required()["observed_files"][path.relative_to(self.workspace).as_posix()] = digest
