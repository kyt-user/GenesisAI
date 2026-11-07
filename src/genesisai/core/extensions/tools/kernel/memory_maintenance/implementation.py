class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        jobs = context.runtime.maintenance
        operation, identity = args['operation'], args.get('maintenance_id')
        if operation in {'refresh', 'rebuild'}:
            if set(args) != {'operation'}:
                raise ValueError('提交维护只需operation')
            identity = jobs.enqueue('project_index' if operation == 'refresh' else 'rebuild')
            return {'maintenance_id': identity, 'state': 'queued'}
        if operation == 'cancel':
            if not identity or set(args) != {'operation', 'maintenance_id'}:
                raise ValueError('取消维护需要maintenance_id')
            return jobs.cancel(identity)
        if args.get('wait_seconds'):
            if not identity:
                raise ValueError('等待维护需要maintenance_id')
            return jobs.wait(identity, args['wait_seconds'])
        return jobs.status(identity, offset=args.get('offset', 0), limit=args.get('limit', 50))
