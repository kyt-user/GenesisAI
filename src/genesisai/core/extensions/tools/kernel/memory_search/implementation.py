class Implementation:
    def prepare(self, context, args):
        return {
            "project_id": context.store.record.project_id,
            "workspace_id": context.store.record.workspace_id,
        }

    def execute(self, context, args):
        return {
            "matches": context.runtime.memory.search(
                args["query"],
                corpus=args.get("corpus", "all"),
                limit=args.get("limit", 8),
            )
        }
