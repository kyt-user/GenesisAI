"""检索可用 Skill 的名称、摘要、来源和覆盖关系。"""

from genesisai.skills.registry import SkillRegistry
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        registry = SkillRegistry(context.access.workspace, context.runtime.registry)
        return {"skills": registry.search(args["query"], args.get("limit", 20)), "diagnostics": registry.diagnostics}



