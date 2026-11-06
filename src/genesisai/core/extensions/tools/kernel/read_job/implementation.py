from genesisai.shared.security import ToolError


class Implementation:
    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
        if payload.get("verification_current") and type(payload.get("passed")) is bool:
            return [Observation("verification", passed=payload["passed"], detail=payload["job_id"])]
        return []

    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        if args.get("output") or args.get("job_id"):
            if not args.get("job_id"):
                raise ToolError("validation_error", "读取输出需要 job_id")
            context.runtime.jobs.wait(args["job_id"], args.get("wait_seconds", 0))
            result = context.runtime.jobs.output(
                args["job_id"], args.get("offset", 0), args.get("limit", 4000) if args.get("output") else 0
            )
            if type(result.get("passed")) is bool:
                context.runtime.tasks.reconcile()
                task = context.runtime.tasks.active_task() or {}
                result["verification_current"] = (result.get("task_id") == task.get("id") and not task.get("drifted_files") and (task.get("last_write_at") or 0) <= result["started"])
                state = context.store.record.run_runtime
                state["pending_verification_jobs"] = [job for job in state.get("pending_verification_jobs", []) if job != args["job_id"]]
            return result
        if args.get("wait_seconds"):
            raise ToolError("validation_error", "等待作业需要 job_id")
        return context.runtime.jobs.status(args.get("job_id"))
