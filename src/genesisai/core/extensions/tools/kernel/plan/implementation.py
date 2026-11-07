"""Read and incrementally update the current task; permissions remain host-owned."""
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args):
        allowed = ({'section', 'offset', 'limit', 'parent', 'status', 'max_chars'} if args['operation'] == 'read'
                   else {'expected_revision', 'upsert', 'remove', 'non_goals'})
        if args.keys() - allowed - {'operation'}:
            raise ToolError('validation_error', '计划读取和更新参数不能混用')
        task=context.runtime.tasks.active_task()
        if not task:
            raise ToolError('project_task_missing', '当前没有活动开发任务')
        if args['operation'] == 'update' and 'expected_revision' not in args:
            raise ToolError('validation_error', '更新计划需要 expected_revision')
        return {'name':'plan', 'target':str(context.store.root), 'task_id':task['id'], 'operation':args['operation'],
                'plan_revision':task['plan_revision']}

    def execute(self, context, args):
        if args['operation'] == 'read':
            return context.runtime.tasks.read(**{key: value for key, value in args.items() if key != 'operation'})
        task = context.runtime.tasks.update_plan(expected_revision=args['expected_revision'], upsert=args.get('upsert'),
                                                  remove=args.get('remove'), non_goals=args.get('non_goals'))
        return {'task_id':task['id'], 'status':task['status'], 'plan_ready':task['plan_ready'],
                'plan_revision':task['plan_revision']}
