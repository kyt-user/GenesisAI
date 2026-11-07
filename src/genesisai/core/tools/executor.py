"""不包含具体业务分支的统一工具执行管线。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from genesisai.core.tools.base import SchemaConfigError, SchemaValidationError, PartialToolResult
from genesisai.core.tools.worker import tool_worker
from genesisai.core.tools.permissions import accepted_tool, current_authorization
from genesisai.shared.results import error_result
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError


class ToolExecutor:
    """统一工具执行管线：参数校验→权限检查→执行→结果封装。"""
    def __init__(self, *, catalog, loader, policy, ledger, context):
        self.catalog = catalog
        self.loader = loader
        self.policy = policy
        self.ledger = ledger
        self.context = context

    def execute(self, call: dict, approved: str | None = None) -> dict:
        started_at = time.monotonic()
        preview = None
        descriptor = None
        execution_started = False
        try:
            state = self.context.store.record
            self._validate_call(call)
            prior = self.ledger.prior(call)
            if prior and prior["state"] in {"succeeded", "failed"}:
                return prior["result"]
            if prior and prior["state"] in {"started", "unknown"}:
                return error_result(
                    call,
                    "unknown_execution",
                    "上次调用结果不确定；禁止自动重复执行",
                )

            if state.status == 'running' and state.deadline and (time.time() >= state.deadline):
                raise ToolError('task_timeout', '任务执行时间已用完，进度已保存')

            descriptor = self.catalog.descriptor_for_execution(call["name"])
            args = json.loads(call["arguments"])
            descriptor.validate_arguments(args)
            implementation = self.loader.load(descriptor)
            deadline = started_at + descriptor.spec.timeout_seconds
            if state.status == "running" and state.deadline:
                deadline = min(deadline, time.monotonic() + state.deadline - time.time())
            invocation_context = replace(self.context, call_id=call['id'])
            with tool_worker(
                invocation_context, implementation, self.context.runtime.observations.adapters.get(descriptor.name), deadline
            ) as worker:
                preview = worker.request("prepare", args)

                if descriptor.spec.awaits_input:
                    waiting = state.run_runtime.get("pending_question", {})
                    if waiting.get("call_id") != call["id"] or "answer" not in waiting:
                        if not prior:
                            self.ledger.await_input(call, preview)
                        return {"call_id": call["id"], "input_request": preview}
                confirmation_required = self.policy.requires_confirmation(descriptor, args, preview) or (
                    prior is not None and prior.get("state") == "prepared"
                    and (approved is not None or self.policy.authorization_for(descriptor, args, preview) is None)
                )
                if confirmation_required:
                    if not isinstance(preview, dict):
                        raise ToolError("permission_error", "需要确认的工具没有提供资源预览")
                    preview = {**preview, "args": args}
                    if approved is None:
                        if not prior or prior.get("state") != "prepared":
                            digest_value = self.policy.freeze(preview)
                            self.ledger.prepare(call, preview, digest_value)
                        return {"pending": True, "call_id": call["id"], "preview": preview}
                    self.policy.validate(preview, prior, approved)

                if cancellation_requested():
                    raise ToolError('user_cancelled', '任务已取消')
                worker.check_deadline()
                self.ledger.start(call)
                execution_started = True
                acceptance = accepted_tool.set(
                    {"name": descriptor.name, "digest": approved}
                    if approved is not None else None
                )
                authorization = self.policy.authorization_for(descriptor, args, preview) if approved is None else None
                authorization_token = current_authorization.set(authorization)
                from genesisai.core.extensions.tools.web.budget import active_scope
                scope_token = active_scope.set({'authorization_id': authorization['id'], 'domains': authorization['domains']}
                                              if authorization is not None else None)
                invocation_context = replace(invocation_context, authorization=authorization)
                try:
                    if descriptor.spec.execution == 'job':
                        from genesisai.core.jobs.tool_operation import submit_tool
                        payload, observations = submit_tool(invocation_context, descriptor, implementation, args, preview)
                    else:
                        worker.request('authorize', (authorization, accepted_tool.get()))
                        payload, observations = worker.request("execute", args)
                finally:
                    active_scope.reset(scope_token)
                    current_authorization.reset(authorization_token)
                    accepted_tool.reset(acceptance)
                if not isinstance(payload, dict):
                    raise ToolError("invalid_result", "工具结果必须是对象")
                result = {
                    "call_id": call["id"],
                    "ok": True,
                    "data": payload,
                    "error": None,
                    "source_refs": payload.get("source_refs", []),
                    "artifact_refs": payload.get("artifact_refs", []),
                    "truncated": bool(payload.get("truncated", False)),
                }
                if descriptor.spec.awaits_input:
                    payload["answer"] = waiting["answer"]
                if isinstance(payload, PartialToolResult):
                    result.update(ok=False, data=dict(payload), error=payload.error)
                result["observations"] = observations
                if field := descriptor.spec.completion_field:
                    candidate = payload.get(field)
                    if not isinstance(candidate, str) or not candidate.strip():
                        raise ToolError("invalid_result", "完成提议需要非空文本")
                    result["completion_candidate"] = candidate
        except Exception as exc:  # noqa: BLE001 - ledger covers all extension failures
            self.context.runtime.logger.error('tool_error', exc, call=call)  # noqa: PLE1205 - structured RunLogger
            call_id = call.get("id", "") if isinstance(call, dict) else ""
            current = self.ledger.store.record.calls.get(call_id, {})
            target = preview.get("target") if isinstance(preview, dict) else None
            if (
                current.get("state") == "started"
                and descriptor is not None
                and descriptor.spec.side_effect
                and getattr(exc, "error_type", None) not in {"command_not_found", "command_cancelled", "mcp_tool_error", "patch_conflict"}
                and (getattr(exc, "error_type", None) in {"unknown_execution", "tool_timeout", "worker_lost", "worker_cancelled"}
                     or (target and Path(target).exists()))
            ):
                result = error_result(
                    call,
                    "unknown_execution",
                    f"{getattr(exc, 'error_type', 'execution_error')}：操作可能已执行，请检查输出，禁止自动重放",
                )
                self.ledger.unknown(call, result)
                return result
            code = getattr(exc, "error_type", None) or (
                "validation_error"
                if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError, SchemaValidationError, SchemaConfigError))
                else "execution_error"
            )
            message = self._safe_message(exc, code)
            result = error_result(call, code, message, bool(getattr(exc, "retryable", False)))
            if code == "duplicate_call_id":
                return result

        if self._can_record(call):
            try:
                self.ledger.finish(
                    call,
                    result,
                    round((time.monotonic() - started_at) * 1000),
                )
            except Exception:
                if execution_started:
                    result = error_result(
                        call,
                        "unknown_execution",
                        "操作已经执行但最终状态无法保存；禁止自动重放",
                    )
                    self.ledger.store.record.calls[call['id']] = {
                        "call": call,
                        "state": "unknown",
                        "result": result,
                        "executed": True,
                    }
                    try:
                        self.ledger.store.commit()
                    except Exception as persist_error:  # noqa: BLE001 - retain the unknown in memory
                        self.context.runtime.logger.error("tool_state_save_error", persist_error, call=call)  # noqa: PLE1205
                    return result
                raise
        return result

    def reject(self, call: dict) -> dict:
        result = error_result(call, "confirmation_rejected", "用户拒绝执行")
        self.ledger.reject(call, result)
        return result

    @staticmethod
    def _validate_call(call: object) -> None:
        if not isinstance(call, dict):
            raise ToolError("invalid_call", "工具调用必须是对象")
        if not isinstance(call.get("id"), str) or not call["id"]:
            raise ToolError("invalid_call", "工具调用 ID 无效")
        if not isinstance(call.get("name"), str) or not call["name"]:
            raise ToolError("invalid_call", "工具名称无效")
        if not isinstance(call.get("arguments"), str):
            raise ToolError("invalid_call", "工具参数必须是 JSON 字符串")

    @staticmethod
    def _can_record(call: object) -> bool:
        return isinstance(call, dict) and isinstance(call.get("id"), str) and bool(call["id"]) and isinstance(call.get("name"), str)

    @staticmethod
    def _safe_message(exc: Exception, code: str) -> str:
        if code == "execution_error":
            return "工具执行失败：" + type(exc).__name__
        return str(exc)[:300] or type(exc).__name__

