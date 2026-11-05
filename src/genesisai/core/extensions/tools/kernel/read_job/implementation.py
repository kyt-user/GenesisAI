from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        if args.get("output"):
            if not args.get("job_id"):
                raise ToolError("validation_error", "读取输出需要 job_id")
            return context.runtime.jobs.output(
                args["job_id"], args.get("offset", 0), args.get("limit", 4000)
            )
        return context.runtime.jobs.status(args.get("job_id"))
