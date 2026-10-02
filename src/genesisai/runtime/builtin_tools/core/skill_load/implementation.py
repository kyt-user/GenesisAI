"""加载最多两个 Skill 正文，推荐工具仍需通过 tool_load 激活。"""

from genesisai.skills.registry import SkillError, SkillRegistry
from genesisai.shared.security import ToolError
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        registry = SkillRegistry(context.access.workspace, context.runtime.registry)
        try:
            names = registry.load(args["names"], context.store.data["tool_runtime"]["active_skills"])
        except SkillError as exc:
            raise ToolError("skill_error", str(exc)) from exc
        context.store.data["tool_runtime"]["active_skills"] = names
        context.store.save()
        return {"loaded": [registry.describe(name, include_content=True) for name in names]}



