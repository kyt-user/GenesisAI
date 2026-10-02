"""校验并组合结构化 Prompt YAML。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files

import yaml


PROMPT_FIELDS = frozenset({"name", "category", "description", "content"})
CONTENT_FIELDS = frozenset({"objective", "instructions", "constraints", "completion"})
PROMPT_CATEGORIES = frozenset({"system", "policy", "protocol", "state", "tool", "completion"})


class PromptFormatError(ValueError):
    """Prompt 文件不符合固定结构。"""


@dataclass(frozen=True)
class ComposedPrompt:
    text: str
    names: list[str]
    hashes: dict[str, str]


class PromptComposer:
    """按身份+政策+协议+状态+工具+完成契约组合系统提示词。"""
    BASE = ("system/identity.prompt.yaml", "system/policies.prompt.yaml")
    PROTOCOLS = {
        "direct_answer": "protocols/direct_answer.prompt.yaml",
        "web_quick": "protocols/web_research.prompt.yaml",
        "web_normal": "protocols/web_research.prompt.yaml",
        "web_deep": "protocols/web_research.prompt.yaml",
        "local_files": "protocols/local_files.prompt.yaml",
    }

    STATE_PROMPTS = {
        "prepare": "states/prepare.prompt.yaml",
        "plan": "states/plan.prompt.yaml",
        "execute": "states/execute.prompt.yaml",
        "await_confirmation": "states/execute.prompt.yaml",
        "verify": "states/verify.prompt.yaml",
        "recover": "states/recover.prompt.yaml",
        "complete": "states/answer.prompt.yaml",
        "partial": "states/answer.prompt.yaml",
        "failed": "states/answer.prompt.yaml",
    }

    EXTRA_PROTOCOLS = {
        "coding": "protocols/coding.prompt.yaml",
        "file_management": "protocols/file_management.prompt.yaml",
        "testing": "protocols/testing.prompt.yaml",
        "git_read": "protocols/git_read.prompt.yaml",
        "office": "protocols/office.prompt.yaml",
        "debugging": "protocols/debugging.prompt.yaml",
        "creative_writing": "protocols/creative_writing.prompt.yaml",
        "revision": "protocols/revision.prompt.yaml",
    }

    def compose(self, profile: str, state: dict, *, recovery: bool = False, final: bool = False) -> ComposedPrompt:
        """根据任务档位和运行状态组装完整的系统提示词。"""
        protocol = self.PROTOCOLS.get(profile, self.PROTOCOLS["direct_answer"])
        lifecycle = "recover" if recovery else ("complete" if final else state.get("lifecycle", "execute"))
        names = [*self.BASE, self.STATE_PROMPTS.get(lifecycle, self.STATE_PROMPTS["execute"]), protocol]
        for item in state.get("protocols", []):
            name = self.EXTRA_PROTOCOLS.get(item)
            if name and name not in names:
                names.append(name)
        if not final and not recovery:
            names.extend(("tools/catalog_usage.prompt.yaml", "tools/skill_usage.prompt.yaml"))
        names.append("tools/evidence_usage.prompt.yaml")
        names.append("completion/recovery.prompt.yaml" if recovery else "completion/final_answer.prompt.yaml")
        root = files("genesisai").joinpath("prompt/templates")
        parts, hashes = [], {}
        seen_prompt_names: set[str] = set()
        for name in names:
            raw = root.joinpath(name).read_text(encoding="utf-8")
            prompt = self.parse(raw, source=name)
            if prompt["name"] in seen_prompt_names:
                raise PromptFormatError(f"Prompt 名称重复：{prompt['name']}")
            seen_prompt_names.add(prompt["name"])
            parts.append(self.render(prompt))
            hashes[name] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now = datetime.now().astimezone()
        runtime = {
            "当前日期": now.date().isoformat(),
            "时区": str(now.tzinfo),
            "profile": profile,
            "phase": "recover" if recovery else ("answer" if final else state.get("phase", "explore")),
            "budget": state.get("budget", {}),
            "usage": state.get("usage", {}),
            "stop_reason": state.get("stop_reason"),
            "当前目标": state.get("objective"),
            "沿用证据": state.get("reuse_evidence", False),
            "工具能力状态": {} if final or recovery else state.get("capabilities", {}),
            "最近搜索诊断": state.get("search_diagnostics", {}),
            "最近资源故障": state.get("failed_resources", {}),
            "输出恢复要求": state.get("response_issue"),
            "开发任务延续": state.get("pending_intent"),
            "已要求实际执行": state.get("execution_requested", False),
            "本轮已产生项目变更": state.get("project_changed", False),
        }
        parts.append("## 可信运行状态\n```json\n" + json.dumps(runtime, ensure_ascii=False, indent=2) + "\n```")
        if final or recovery:
            parts.append("当前请求禁止调用任何工具。请立即交付简洁、可读、带真实 source_ref 的答案。")
        return ComposedPrompt("\n\n".join(parts), names, hashes)

    @staticmethod
    def parse(raw: str, *, source: str = "<prompt>") -> dict:
        """解析并校验单个 Prompt YAML 文件的结构。"""
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise PromptFormatError(f"Prompt YAML 无效：{source}") from exc
        if not isinstance(value, dict) or set(value) != PROMPT_FIELDS:
            raise PromptFormatError(f"Prompt 顶层字段无效：{source}")
        for field in ("name", "category", "description"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise PromptFormatError(f"Prompt {field} 必须是非空字符串：{source}")
        if value["category"] not in PROMPT_CATEGORIES:
            raise PromptFormatError(f"Prompt category 无效：{source}")
        content = value["content"]
        if not isinstance(content, dict) or set(content) != CONTENT_FIELDS:
            raise PromptFormatError(f"Prompt content 字段无效：{source}")
        if not isinstance(content["objective"], str) or not content["objective"].strip():
            raise PromptFormatError(f"Prompt objective 必须是非空字符串：{source}")
        for field in ("instructions", "constraints", "completion"):
            items = content[field]
            if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
                raise PromptFormatError(f"Prompt {field} 必须是字符串数组：{source}")
        return value

    @staticmethod
    def render(prompt: dict) -> str:
        """将解析后的 Prompt 字典渲染为 Markdown 文本。"""
        content = prompt["content"]
        lines = [f"## {prompt['description']}", f"目标：{content['objective']}"]
        labels = (("instructions", "指令"), ("constraints", "约束"), ("completion", "完成条件"))
        for field, label in labels:
            if content[field]:
                lines.append(f"{label}：")
                lines.extend(f"- {item}" for item in content[field])
        return "\n".join(lines)
