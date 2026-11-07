from genesisai.shared.security import ToolError


class Implementation:
    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
        if payload.get('kind') == 'builtin_tool':
            return [Observation(**item) for item in payload.get('tool_observations', [])]
        if payload.get("verification_current") and type(payload.get("passed")) is bool:
            return [Observation("verification", passed=payload["passed"], detail=payload["job_id"])]
        return []

    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        if args.get('input_id'):
            import time
            from genesisai.shared.run_control import cancellation_requested
            from genesisai.shared.time_limits import tool_deadline
            if not args.get('job_id'):
                raise ToolError('validation_error', '输入回执需要 job_id')
            deadline = min(time.monotonic() + args.get('wait_seconds', 0), tool_deadline.get() or float('inf'))
            while True:
                receipt = context.runtime.jobs.input_receipt(args['job_id'], args['input_id'])
                if receipt['status'] not in {'prepared', 'accepted', 'sending'} or time.monotonic() >= deadline or cancellation_requested():
                    return {'job_id': args['job_id'], 'input': receipt,
                            'job_status': context.runtime.jobs.status(args['job_id'])['status']}
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
        if args.get("output") or args.get("job_id"):
            if not args.get("job_id"):
                raise ToolError("validation_error", "读取输出需要 job_id")
            context.runtime.jobs.wait(args["job_id"], args.get("wait_seconds", 0))
            result = context.runtime.jobs.output(
                args["job_id"], args.get("offset", 0), args.get("limit", 4000) if args.get("output") else 0
            )
            if result["status"] not in {"queued", "running", "stopping"}:
                state = context.store.record.run_runtime
                if result['status'] != 'unknown' and result.get('kind') == 'command':
                    state["pending_execution_jobs"] = [job for job in state.get("pending_execution_jobs", []) if job != args["job_id"]]
                if result.get('kind') != 'command':
                    if result['kind'] == 'builtin_tool':
                        completed = context.runtime.collect_tool_job(args['job_id'], context.call_id)
                        result['tool_observations'] = completed['observations']
                        result['operation_error'] = completed.get('operation_error')
                        completed = completed['payload']
                        result['source_refs'] = completed.get('source_refs', [])
                        result['artifact_refs'] = completed.get('artifact_refs', [])
                    elif result['kind'] == 'external_tool':
                        try:
                            completed = context.runtime.capabilities.collect_tool_result(args['job_id'])
                        except ToolError as exc:
                            from genesisai.core.tools.base import PartialToolResult
                            diagnostics = context.runtime.capabilities.collect_tool_logs(args['job_id'])
                            return PartialToolResult(exc.error_type, str(exc), **result, **diagnostics)
                    else:
                        completed = context.runtime.jobs.operation_result(args['job_id'])
                    if result['kind'] == 'mcp_request':
                        completed = context.runtime.mcp.job_result(completed)
                    result['result'] = completed
                    if result['kind'] == 'builtin_tool':
                        from genesisai.core.tools.base import PartialToolResult
                        if isinstance(completed, PartialToolResult):
                            result['operation_error'] = completed.error
                            return PartialToolResult(completed.error['code'], completed.error['message'], **result)
                        if error := result['operation_error']:
                            failure = PartialToolResult(error['code'], error['message'], **result)
                            failure.error = error
                            return failure
                    if result['kind'] == 'capability_test':
                        item = context.runtime.capabilities.store.get(completed['id'])
                        result['verification_current'] = (item['revision'] == completed['revision'] and item['digest'] == completed['digest']
                            and context.runtime.capabilities.verification_current(item))
                        result['passed'] = completed['report']['passed']
            if type(result.get("passed")) is bool:
                context.runtime.tasks.reconcile()
                task = context.runtime.tasks.active_task() or {}
                if result.get('kind') == 'command':
                    result["verification_current"] = (result.get("task_id") == task.get("id") and not task.get("drifted_files") and (task.get("last_write_at") or 0) <= result["started"])
                state = context.store.record.run_runtime
                state["pending_verification_jobs"] = [job for job in state.get("pending_verification_jobs", []) if job != args["job_id"]]
            return result
        if args.get("wait_seconds"):
            raise ToolError("validation_error", "等待作业需要 job_id")
        return context.runtime.jobs.status(offset=args.get("offset", 0), limit=args.get("limit", 50))
