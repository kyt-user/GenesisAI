class Implementation:
    def prepare(self, context, args):
        return {
            "project_id": context.store.record.project_id,
            "workspace_id": context.store.record.workspace_id,
        }

    def execute(self, context, args):
        offset, limit = args.get('offset', 0), args.get('limit', 8)
        matches = context.runtime.memory.search(args['query'], corpus=args.get('corpus', 'all'),
                                                limit=limit + 1, offset=offset)
        return {
            "matches": matches[:limit],
            "next_offset": offset + limit if len(matches) > limit else None,
        }
