"""不包含具体业务分支的统一工具执行管线。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from genesisai.shared.results import error_result
from genesisai.shared.security import ToolError
from genesisai.core.tools.base import SchemaConfigError, SchemaValidationError, Tool


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

            descriptor = self.catalog.descriptor_for_execution(call["name"])
            args = json.loads(call["arguments"])
            Tool(
                name=descriptor.name,
                description=descriptor.description,
                parameters=descriptor.spec.parameters,
            )._validate_args(args)
            implementation = self.loader.load(descriptor)
            preview = implementation.prepare(self.context, args)

            confirmation_required = self.policy.requires_confirmation(descriptor.spec) or (
                prior is not None and prior.get("state") == "prepared"
            )
            if confirmation_required:
                if not isinstance(preview, dict):
                    raise ToolError("permission_error", "需要确认的工具没有提供资源预览")
                if approved is None:
                    if not prior or prior.get("state") != "prepared":
                        digest_value, expires = self.policy.freeze(preview)
                        self.ledger.prepare(call, preview, digest_value, expires)
                    return {"pending": True, "call_id": call["id"], "preview": preview}
                self.policy.validate(preview, prior, approved)

            self.ledger.start(call)
            self.ledger.store.data['calls'][call['id']]['executed'] = True
            execution_started = True
            payload = implementation.execute(self.context, args)
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
        except Exception as exc:
            call_id = call.get("id", "") if isinstance(call, dict) else ""
            current = self.ledger.store.data["calls"].get(call_id, {})
            target = preview.get("target") if isinstance(preview, dict) else None
            if (
                current.get("state") == "started"
                and descriptor is not None
                and descriptor.spec.side_effect
                and getattr(exc, "error_type", None) != "command_not_found"
                and target
                and Path(target).exists()
            ):
                result = error_result(
                    call,
                    "unknown_execution",
                    "操作可能已执行但登记未完成；请检查输出，禁止自动重放",
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
                    self.ledger.store.data["calls"][call["id"]] = {
                        "call": call,
                        "state": "unknown",
                        "result": result,
                    }
                    try:
                        self.ledger.store.save()
                    except Exception:
                        pass
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

