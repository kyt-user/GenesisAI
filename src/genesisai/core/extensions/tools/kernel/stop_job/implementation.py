class Implementation:
    def prepare(self, context, args):
        info = context.runtime.jobs.status(args["job_id"])
        return {"target": args["job_id"], **{key: info.get(key) for key in ("kind", "command", "cwd", "started", "task_id")}}

    def execute(self, context, args):
        return context.runtime.jobs.stop(args["job_id"])
