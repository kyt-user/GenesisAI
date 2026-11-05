class Implementation:
    def prepare(self, context, args):
        return {"target": args["job_id"], **context.runtime.jobs.status(args["job_id"])}

    def execute(self, context, args):
        return context.runtime.jobs.stop(args["job_id"])
