class Implementation:
    def prepare(self, context, args):
        return {
            "project_id": context.store.record.project_id,
            "workspace_id": context.store.record.workspace_id,
        }

    def execute(self, context, args):
        return context.runtime.memory.write_note(**args)
