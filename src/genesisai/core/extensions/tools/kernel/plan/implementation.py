"""Write a structured task plan through the application task service."""
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args):
        task=context.runtime.tasks.active_task()
        if not task:
            raise ToolError('project_task_missing', '当前没有活动开发任务')
        return {'name':'plan', 'target':str(context.store.root), 'task_id':task['id'], 'steps':len(args['steps'])}

    def execute(self, context, args):
        task=context.runtime.tasks.set_plan(steps=args['steps'], acceptance=args['acceptance'], non_goals=args.get('non_goals',[]))
        return {'task_id':task['id'], 'status':task['status'], 'plan_ready':task['plan_ready']}
