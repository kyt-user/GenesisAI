class Implementation:
    def prepare(self, context, args):
        return context.runtime.jobs.prepare_input(args, context.call_id)

    def execute(self, context, args):
        return context.runtime.jobs.write_input(args, context.call_id)
