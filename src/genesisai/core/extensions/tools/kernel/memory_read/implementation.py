class Implementation:
    def prepare(self, context, args):
        return {
            "project_id": context.store.record.project_id,
            "workspace_id": context.store.record.workspace_id,
        }

    def execute(self, context, args):
        reference = args['ref']
        source = context.store.record.sources.get(reference, {})
        if reference.startswith('artifact_') or source.get('kind') == 'capability_result':
            if args.get('revision') is not None or args.get('view', 'record') != 'record':
                from genesisai.shared.security import ToolError
                raise ToolError('validation_error', '不可变能力结果仅提供 record 正文视图，不接受知识 revision')
            return context.runtime.capabilities.read_tool_result(reference, offset=args.get('offset', 0),
                limit=args.get('limit', 200000), page_tokens=context.runtime.context_budgeter.read_page_tokens())
        return context.runtime.memory.read(
            args["ref"],
            offset=args.get("offset", 0),
            limit=args.get("limit", 200000),
            revision=args.get("revision"),
            view=args.get('view', 'record'),
            page_tokens=context.runtime.context_budgeter.read_page_tokens(),
        )
