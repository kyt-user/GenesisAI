"""内核工具：一次调用返回所请求 Skill 的指令正文。

Skills 缝的可执行侧：模型据返回的 SKILL.md 正文推进任务，替代原先
`skill_search / skill_describe / skill_load` 三个只读元工具。
"""

from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        registry = getattr(context.runtime, "skills", None)
        if registry is None:
            raise ToolError("skill_unavailable", "Skill 注册表不可用")
        name = args["name"]
        if name not in registry.items:
            raise ToolError("unknown_skill", f"未知 Skill：{name}")
        item = registry.describe(name, include_content=True)
        return {
            "name": item["name"],
            "description": item["description"],
            "tools": item["tools"],
            "content": item["content"],
        }