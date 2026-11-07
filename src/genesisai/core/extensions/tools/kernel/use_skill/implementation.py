"""Load an explicit stage and its dependency closure into the session."""
from genesisai.core.extensions.skills.registry import SkillError
from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        registry = context.runtime.skills
        name, stage = args['name'], args['stage']
        if name not in registry.items:
            raise ToolError('unknown_skill', '未知 Skill：' + name)
        active = dict(context.store.record.tool_runtime['active_skills'])
        if args.get('operation', 'load') == 'unload':
            if active.get(name) == stage:
                active.pop(name)
            context.store.record.tool_runtime['active_skills'] = active
            context.store.commit()
            return {'name': name, 'stage': stage, 'active': name in active}
        available = {item.name for item in context.runtime.catalog.available_tools()}.__contains__
        try:
            resolved = registry.resolve(name, stage, available=available)
            context.store.record.tool_runtime['active_skills'] = registry.load(
                name, stage, active, available=available)
        except SkillError as exc:
            raise ToolError('skill_dependency', str(exc)) from exc
        context.store.commit()
        item = registry.describe(name, include_content=True)
        return {'name': name, 'description': item['description'], **resolved,
                'content': item['content'], 'active': True}
