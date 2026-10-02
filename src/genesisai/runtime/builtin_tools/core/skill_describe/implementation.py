"""查看一个 Skill 的来源、哈希、工具依赖和覆盖关系。"""

from genesisai.skills.registry import SkillError, SkillRegistry
from genesisai.shared.security import ToolError
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        try: return SkillRegistry(context.access.workspace, context.runtime.registry).describe(args["name"])
        except SkillError as exc: raise ToolError("skill_error", str(exc)) from exc



