"""单 Agent 的 ReAct 循环骨架。

日志记录器通过 runtime 注入。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from dataclasses import asdict

from genesisai.agent.completion import CompletionValidator
from genesisai.agent.progress import ProgressTracker
from genesisai.shared.retry_policy import RetryPolicy
from genesisai.shared.request_timeouts import RequestTimeouts
from genesisai.agent.evidence import EvidenceBundle
from genesisai.agent.recovery import RecoveryMixin, SOURCE_REF
from genesisai.agent.state_machine import RunStateMachine, StateTransitionError
from genesisai.shared.budgets import (
    initial_run_runtime,
    is_recall_request,
)
from genesisai.shared.ids import uid
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.time_limits import (DEFAULT_TASK_SECONDS,
                                         DEFAULT_MAX_ROUNDS, DEFAULT_MAX_TOOLS, PROGRESS_CHECKPOINT_ROUNDS, exhausted, remaining)
from genesisai.shared.messages import UserInput, content_text


class ContextLimitError(ValueError):
    """当前与上一轮必要上下文无法安全放入模型请求。"""


class Runner(RecoveryMixin):
    """可持久化的单智能体 ReAct 循环。

    研究和完成策略由独立确定性模块约束；
    工具调用通过 ToolRuntime 执行，结果写回会话存储。
    """

    def __init__(
        self,
        model,
        runtime,
        *,
        max_rounds=DEFAULT_MAX_ROUNDS,
        max_tools=DEFAULT_MAX_TOOLS,
        token_budget=None,
        seconds=DEFAULT_TASK_SECONDS,
        request_timeouts=None,
        on_event=None,
    ):
        self.model, self.runtime, self.store = model, runtime, runtime.store
        self.max_rounds, self.max_tools, self.seconds = max_rounds, max_tools, seconds
        self.token_budget = token_budget
        if any(v is not None and (type(v) is not int or v < 1) for v in (max_rounds, max_tools, token_budget)):
            raise ValueError("显式调用/Token预算必须为正整数")
        if seconds is not None and (type(seconds) not in (int, float) or seconds <= 0):
            raise ValueError("显式任务时限必须为正数")
        RequestTimeouts.parse(request_timeouts)
        self.timeout_overrides = (request_timeouts.as_dict() if isinstance(request_timeouts, RequestTimeouts)
                                  else dict(request_timeouts or {}))
        self.on_event = on_event or (lambda *args: None)
        self.cancel_requested = False
        self._needs_verification = False
        self.composer = runtime.composer
        self.context_budgeter = runtime.context_budgeter
        self.completion = CompletionValidator()
        self.trace = runtime.trace
        self.machine = RunStateMachine(self.store)
        self.tasks = runtime.tasks
        self.run_logger = runtime.logger
        self.set_model(model)

    def set_model(self, model):
        self.model = model
        self.retry_policy = RetryPolicy.parse(getattr(model, 'retry_config', None))
        configured = RequestTimeouts.parse(getattr(model, 'request_timeouts', None)).as_dict()
        self.request_timeouts = RequestTimeouts.parse({**configured, **self.timeout_overrides})
        self.store.record.run_runtime['request_timeouts'] = self.request_timeouts.as_dict()
        self.runtime.bind_model(model)
        if self.context_budgeter is not None:
            self.context_budgeter.configure(
                getattr(model, "context_config", None),
                output_tokens=getattr(model, "generation", {}).get("max_tokens"),
                summarize=self._summarize_context,
                tools=self.runtime.definitions,
                estimation_key=f"{type(model).__name__}:{getattr(model, 'model', '')}",
            )
        self.runtime.supports_images = (
            getattr(getattr(model, "image_capability", None), "supported", None) is True
        )
        if getattr(self.runtime, "images", None) is not None:
            self.runtime.images.config = self.context_budgeter.config
            if model is not None:
                model.image_resolver = self.runtime.images.data_url

    @property
    def research(self):
        return self.runtime.research

    @property
    def development(self):
        """开发验证策略端口（core/development 实现，由 app 注入）。"""
        return self.runtime.development

    @property
    def prompt(self):
        """任务协议选择端口（core/prompt 实现，由 app 注入）。"""
        return self.runtime.prompt

    def status(self, value, answer=None):
        """更新并持久化运行状态，同时同步状态机转换。"""
        self.store.record.status = value
        if answer is not None:
            self.store.record.answer = answer
        target = {
            "completed": "complete",
            "partial": "partial",
            "limit_reached": "partial",
            "failed": "failed",
            "cancelled": "cancelled",
            "awaiting_input": "await_input",
            "awaiting_confirmation": "await_confirmation",
            "interrupted": "interrupted",
        }.get(value)
        if target and self.machine.state != target:
            try:
                self.machine.transition(target, "status_" + value, save=False)
            except StateTransitionError:
                self.store.record.run_runtime["lifecycle"] = target
        if value in {"completed", "partial", "limit_reached", "failed", "cancelled"}:
            self.store.record.run_runtime["phase"] = "done"
            self.store.record.last_run_outcome = {
                "run_id": self.store.record.run_id,
                "status": value,
                "stop_reason": self.store.record.run_runtime.get("stop_reason"),
                "diagnostic": self.store.record.run_runtime.get("last_failure"),
                "tool_activity": self.store.record.run_runtime.get("tool_activity", []),
            }
            self.tasks.finish(value, answer or "")
        if value in {"awaiting_confirmation", "awaiting_input"}:
            self.store.record.run_runtime.setdefault("confirmation_paused_at", None)
            if self.store.record.run_runtime["confirmation_paused_at"] is None:
                self.store.record.run_runtime["confirmation_paused_at"] = time.time()
        self.store.commit()
        state = self.store.record.run_runtime
        self.run_logger.record(
            "run_status", status=value, answer=answer, pending=self.store.record.pending
        )
        self.store.event(
            "status",
            status=value,
            profile=state["profile"],
            phase=state["phase"],
            stop_reason=state.get("stop_reason"),
            evidence_count=len(state["evidence_refs"]),
        )

        # 记录运行摘要日志
        if value in {"completed", "partial", "limit_reached", "failed", "cancelled"}:
            self.run_logger.log_round_summary(
                getattr(self.store.record, "rounds", 0), value, answer
            )

        return dict(
            status=value,
            answer=getattr(self.store.record, "answer", ""),
            pending=self.store.record.pending,
        )

    @staticmethod
    def check_available(d):
        """检查是否可以开始新运行；存在未完成运行时抛出异常。"""
        if d.status in {"running", "awaiting_confirmation", "awaiting_input", "interrupted"}:
            raise ValueError("先 /resume、处理确认或 /cancel，再提交新输入")
        if any(
            (
                call.get("state") in {"started", "unknown"}
                and (not call.get("acknowledged"))
                for call in d.calls.values()
            )
        ):
            raise ValueError(
                "存在执行结果不确定的调用；请检查输出并 /cancel，禁止开始新一轮"
            )

    def ensure_available(self):
        self.check_available(self.store.record)

    def start(self, text, *, on_registered=None):
        """根据用户输入初始化并启动一轮新运行。"""
        d = self.store.record
        input_id = self.run_logger.record(
            "user_input", text=text, previous_run_id=getattr(d, "run_id")
        )
        self.ensure_available()
        request = UserInput(text) if isinstance(text, str) else text
        if not isinstance(request, UserInput) or not isinstance(request.text, str):
            raise ValueError("输入必须为文字或 UserInput")
        text = request.text
        if not text.strip() and not request.images:
            raise ValueError("输入不能为空")
        content = text
        if request.images:
            if not self.runtime.supports_images:
                raise ValueError(
                    "当前模型尚未确认支持图片；请通过 /model 选择图片模型，附件已保留"
                )
            config = self.context_budgeter.config
            if sum(p.storage_bytes for p in request.images) > config.max_pending_image_bytes:
                raise ValueError('原图与预览超过附件内存配额')
            parts = [self.runtime.images.import_image(p) for p in request.images]
            content = ([{"type": "text", "text": text}] if text else []) + parts
        self.store.verify_sources()
        # Stage the new turn until its first atomic session save succeeds.
        previous_data = d
        d = d.evolve(history=list(d.history), messages=list(d.messages))
        previous_runtime = getattr(d, "run_runtime") or {}
        previous_answer = getattr(d, "answer", "")
        previous_status = getattr(d, "status")
        if d.run_id:
            d.history.append(
                {
                    "run_id": d.run_id,
                    "status": d.status,
                    "answer": getattr(d, "answer", ""),
                    "evidence_refs": list(previous_runtime.get("evidence_refs", [])),
                    "rounds": getattr(d, "rounds", 0),
                    "tool_count": getattr(d, "tool_count", 0),
                    "usage": json.loads(json.dumps(getattr(d, "usage", {}))),
                    "runtime_usage": json.loads(
                        json.dumps(previous_runtime.get("usage", {}))
                    ),
                    "stop_reason": previous_runtime.get("stop_reason"),
                    "tool_activity": json.loads(
                        json.dumps(previous_runtime.get("tool_activity", []))
                    ),
                    "context_report": json.loads(
                        json.dumps(previous_runtime.get("context_report", {}))
                    ),
                }
            )
        project_manager = self.tasks
        active_task = self.tasks.active_task()
        if active_task and active_task['status'] not in {'completed', 'cancelled', 'superseded'}:
            previous_runtime = {**previous_runtime, **active_task['execution_context']}
        elif active_task:
            previous_runtime = {**previous_runtime, 'constraints': {}}
        try:
            self.runtime.prepare_turn()
        except (OSError, ValueError) as exc:
            self.run_logger.error("project_index_failed", exc)
        route = self.prompt.route(text, active_task, previous_runtime)
        continuation = route["development_continuation"]
        profile = route["profile"]
        d.run_runtime = initial_run_runtime(profile)
        d.run_runtime.update(route)
        protocols = route["protocols"]
        if continuation:
            inherited = previous_runtime.get("protocols") or []
            protocols = list(dict.fromkeys([*inherited, *protocols, "coding"]))
            d.run_runtime["pending_intent"] = {
                "kind": "continue_development",
                "task_id": active_task["id"],
                "objective": active_task["objective"],
                "task_status": active_task["status"],
                "current_phase": active_task["status"],
                "default_plan_confirmed": self.prompt.accepts_default_plan(text),
                "next_action": (
                    "write_plan_and_implement"
                    if not active_task.get("plan_ready")
                    else (
                        "verify_changes"
                        if active_task["status"] == "needs_verification"
                        else "implement_next_step"
                    )
                ),
                "awaiting_confirmation": False,
                "instruction": text.strip(),
            }
        d.run_runtime["protocols"] = protocols
        if route.get("followup") and previous_runtime.get("capability_target"):
            d.run_runtime["capability_target"] = previous_runtime["capability_target"]
        d.run_runtime["budget"]["model_calls"] = self.max_rounds
        d.run_runtime["budget"].update(tools=self.max_tools, total_tokens=self.token_budget)
        if previous_runtime.get("evidence_refs") or (route.get("followup") and previous_runtime.get("candidates")):
            # 普通追问建立新 Run，但从上一 Run 搬运不可变候选、来源和证据索引。
            for key in (
                "candidates",
                "candidate_count",
                "content_hashes",
                "evidence",
                "evidence_refs",
            ):
                if key in previous_runtime:
                    d.run_runtime[key] = json.loads(
                        json.dumps(previous_runtime[key], ensure_ascii=False)
                    )
            d.run_runtime["reuse_evidence"] = bool(previous_runtime.get("evidence_refs"))
        d.run_runtime["execution_seconds_limit"] = self.seconds
        d.run_runtime["request_timeouts"] = self.request_timeouts.as_dict()
        d.run_runtime["objective"] = (
            active_task["objective"]
            if continuation
            else (previous_runtime.get("objective") if route.get("followup") and "capability_builder" in protocols else (text or content_text(content, images=True)))
        )
        d.run_runtime["execution_requested"] = route["execution_requested"]
        d.run_runtime["project_changed"] = False
        d.run_runtime["verification_succeeded"] = False
        if is_recall_request(text):
            d.run_runtime["history_lookup"] = True
        d.apply(
            run_id=uid("run"),
            status="running",
            rounds=0,
            tool_count=0,
            failures=0,
            deadline=time.time() + self.seconds if self.seconds is not None else 0,
            pending=[],
            answer="",
            usage={},
            limits=dict(
                rounds=self.max_rounds, tools=self.max_tools, total_tokens=self.token_budget
            ),
        )
        d.messages.append(dict(role="user", content=content))
        self.store.record = d
        d = self.store.record
        try:
            self.store.commit()
        except BaseException:
            self.store.record = previous_data
            raise
        try:
            if on_registered:
                on_registered(d.run_id)
            self.machine.restart()
            self.machine.transition("execute", "prepared")
            self.cancel_requested = False
            if project_manager is not None:
                try:
                    task = project_manager.begin_task(
                        d.run_runtime["objective"], run_id=d.run_id, instruction=text,
                        new_task=route.get('new_task', False), constraint_changes=route.get('constraint_updates', []),
                        execution_requested=route['execution_requested'],
                    )
                    d.run_runtime['objective'] = task['objective']
                    d.run_runtime['constraints'] = project_manager.effective_constraints(task, d.run_id)
                    d.run_runtime.update(project_changed=bool(task['last_write_at']),
                        verification_succeeded=task['verification_passed'], verification_required=task['verification_required'],
                        change_revision=task['change_revision'], verified_revision=task['verification_revision'],
                        changed_files=dict(task['changed_files']))
                    if d.run_runtime['constraints'].get('read_only'):
                        d.run_runtime['execution_requested'] = False
                    d.run_runtime["project_task_id"] = task["id"]
                    d.run_runtime["project_task_status"] = task["status"]
                except Exception as exc:
                    return self.fail(exc, reason='task_state_error')
            self.research.register_user_urls(text)
            self.store.commit()
            self.store.event("run_start", profile=profile, phase="explore")
            self.run_logger.record("run_start", input_event_id=input_id, text=text)
            if cancellation_requested():
                return self.cancel()
            if (
                is_recall_request(text)
                and previous_answer
                and self._can_reuse_answer(text, previous_answer)
            ):
                # 纯文本追问不应让随机模型修改已通过来源和价格校验的事实。
                d.messages.append(
                    dict(role="assistant", content=previous_answer, tool_calls=None)
                )
                recall_status = (
                    "completed" if previous_status == "completed" else "partial"
                )
                d.run_runtime["stop_reason"] = (
                    "model_answer" if recall_status == "completed" else "evidence_gap"
                )
                self.store.commit()
                return self.status(recall_status, previous_answer)
        except KeyboardInterrupt:
            return self.cancel()
        except Exception as exc:
            return self.fail(exc)
        return self.drive()

    def context(self, *, recovery=False, final=False, tools=None):
        """构建当前轮次的模型上下文。"""
        try:
            if tools is None:
                tools = self.runtime.definitions()
            self.store.record.run_runtime["capabilities"] = {
                "enabled": [item["function"]["name"] for item in tools],
            }
            return self.context_budgeter.build(
                self.store.record.run_runtime["profile"], recovery=recovery, final=final
            )
        except ValueError as exc:
            raise ContextLimitError(str(exc)) from exc

    def append_result(self, call, result):
        """记录工具执行结果到会话消息和工具活动日志。"""
        d = self.store.record
        self.run_logger.record("tool_result_delivery", call=call, result=result)
        entry = d.calls.get(call["id"], {})
        executed = entry.get("executed")
        if executed is None and entry.get("state") not in {"unknown", "started"}:
            executed = False
        d.run_runtime.setdefault("tool_activity", []).append(
            {
                "call_id": call["id"],
                "tool": call["name"],
                "ok": result.get("ok"),
                "code": (result.get("error") or {}).get("code"),
                "executed": executed,
            }
        )
        d.messages.append(
            dict(role="tool", tool_call_id=call["id"], content=self.runtime.model_content(call, result))
        )
        d.failures = 0 if result["ok"] else d.failures + 1
        if not ProgressTracker(d.run_runtime).observe(call, result):
            d.run_runtime["stalled_action"] = call["name"]
        d.pending.pop(0)
        if d.run_runtime.get("pending_question", {}).get("call_id") == call["id"]:
            d.run_runtime.pop("pending_question")
        if result.get("ok") and not d.pending and result.get("completion_candidate"):
            d.run_runtime["completion_candidate"] = result["completion_candidate"]
        self._update_development_state(call, result)
        self.runtime.deliver_observations(
            result.get("observations", []), call_id=call["id"]
        )
        self.store.commit()
        self.on_event("tool_result", result)

    def close_pending(self, code):
        """以指定错误码关闭所有待处理调用。"""
        while self.store.record.pending:
            call = self.store.record.pending[0]
            entry = self.store.record.calls.get(call["id"], {})
            result = entry.get("result")
            if result is None:
                if entry.get("state") in {"started", "unknown"}:
                    result = self.runtime.error(
                        call,
                        "unknown_execution",
                        "工具执行结果不确定，可能已产生修改，请检查输出。",
                    )
                else:
                    result = self.runtime.error(
                        call, code, "本次运行已停止，工具未执行；原因：" + code
                    )
            if entry.get("state") == "awaiting_input":
                self.runtime.cancel_question(call, result)
            self.append_result(call, result)

    def stop(self, status, message, reason=None):
        """以指定状态和消息终止当前运行。"""
        state = self.store.record.run_runtime
        state["stop_reason"] = reason or state.get("stop_reason") or status
        self.close_pending(state["stop_reason"])
        return self.status(status, message)

    def fail(self, exc, *, reason="runtime_error"):
        """记录异常诊断并终止运行。"""
        self.run_logger.error("run_error", exc, reason=reason)
        diagnostic = {
            "id": uid("diagnostic"),
            "type": type(exc).__name__,
            # 不持久化异常参数、局部变量、源码文本或 HTTP 响应体。
            "message": "模型服务调用失败"
            if reason == "model_error"
            else "运行组件发生内部异常",
            "frames": [
                {
                    "file": Path(frame.filename).name,
                    "function": frame.name,
                    "line": frame.lineno,
                }
                for frame in traceback.extract_tb(exc.__traceback__)[-12:]
            ],
        }
        self.store.record.run_runtime["last_failure"] = diagnostic
        pending = self.store.record.pending
        self.store.event(
            "failure",
            code=type(exc).__name__,
            diagnostic=diagnostic,
            tool_call_id=pending[0]["id"] if pending else None,
        )
        message = (
            self.model_error(exc) + " 诊断编号：" + diagnostic["id"] + "（/trace 查看）"
        )
        return self.stop("failed", message, reason)

    def cancel(self):
        """标记取消请求并终止运行。"""
        self.run_logger.record("run_cancel")
        self.cancel_requested = True
        self.runtime.cancel_jobs()
        for call in self.store.record.calls.values():
            if call.get("state") in {"started", "unknown"}:
                call["acknowledged"] = True
        return self.stop(
            "cancelled",
            "运行已取消；不确定写操作不会自动重做，请检查输出文件。",
            "user_cancelled",
        )

    def resume(self):
        """从中断或确认等待状态恢复运行。"""
        d = self.store.record
        self.run_logger.record("run_resume", previous_status=d.status)
        if any(
            (
                c["state"] in {"started", "unknown"} and (not c.get("acknowledged"))
                for c in d.calls.values()
            )
        ):
            return self.status(
                "interrupted", "存在不确定调用。请检查输出并 /cancel；不会自动重放。"
            )
        if d.status == "awaiting_input":
            return self.status("awaiting_input")
        if d.status not in {"interrupted", "awaiting_confirmation"}:
            raise ValueError("没有可恢复的运行")
        self.store.verify_sources()
        if d.status == "awaiting_confirmation":
            return self.status("awaiting_confirmation")
        if self.machine.state == "ready":
            # The host can stop just after the first durable user-message checkpoint.
            self.machine.restart()
        self.machine.transition("execute", "resume")
        return self.drive()

    @staticmethod
    def _resume_clock(d):
        state = d.run_runtime
        paused_at = state.get("confirmation_paused_at")
        if paused_at is not None:
            duration = max(0, time.time() - paused_at)
            if d.deadline:
                d.deadline += duration
            state["paused_seconds"] = state.get("paused_seconds", 0) + duration
            state["confirmation_paused_at"] = None

    def answer(self, text):
        d = self.store.record
        question = d.run_runtime.get("pending_question", {})
        if d.status != "awaiting_input" or not d.pending or question.get("call_id") != d.pending[0]["id"]:
            raise ValueError("当前没有待回答的问题")
        text = text.strip()
        if not text or len(text) > 8000:
            raise ValueError("答案长度须为 1 至 8000 个字符")
        options = question.get("options", [])
        if text in {str(i) for i in range(1, len(options) + 1)}:
            text = options[int(text) - 1]
        question["answer"] = text
        self._resume_clock(d)
        d.status = "running"
        d.answer = ""
        self.machine.transition("execute", "user_answer", save=False)
        self.store.commit()
        return self.drive()

    def confirm(self, approve):
        """处理用户对待确认调用的批准或拒绝。"""
        d = self.store.record
        self.run_logger.record(
            "confirmation_decision", approved=approve, pending=d.pending
        )
        if d.status != "awaiting_confirmation" or not d.pending:
            raise ValueError("没有待确认调用")
        try:
            return self._confirm(approve)
        except KeyboardInterrupt:
            return self.cancel()
        except Exception as exc:
            return self.fail(exc)

    def _confirm(self, approve):
        """执行确认或拒绝操作；具体资源仍由执行器重新核对。"""
        d = self.store.record
        self._resume_clock(d)
        call = d.pending[0]
        if d.deadline and time.time() >= d.deadline:
            return self.stop("limit_reached", "运行已超时；确认没有执行", "deadline")
        if cancellation_requested():
            return self.cancel()
        if approve:
            prior = d.calls.get(call["id"], {})
            result = self._execute_tool_call(call, approved=prior.get("digest", "invalid"))
        else:
            result = self.research.after_execute(call, self.runtime.reject(call))
        self.machine.transition("execute", "confirmation_resolved")
        if result.get("error") and result["error"]["code"] == "unknown_execution":
            if cancellation_requested():
                return self.cancel()
            return self.status("interrupted", result["error"]["message"])
        self.append_result(call, result)
        if not approve:
            self.research.force_answer("user_rejected")
            self.close_pending("user_rejected")
        return self.drive()

    def drive(self):
        """执行一轮完整的 ReAct 循环。

        按 思考→行动→观察 迭代，直到产出最终回答或触发限制。
        返回最终状态字典或回答文本；异常由调用方捕获。
        """
        d = self.store.record
        self.status("running")
        try:
            while True:
                if self.cancel_requested or cancellation_requested():
                    return self.cancel()
                state = d.run_runtime
                limits = d.limits
                if d.deadline and time.time() >= d.deadline:
                    return self.finish_timeout(task_expired=True)
                if exhausted(limits.get("total_tokens"), d.usage.get("total_tokens", 0)):
                    return self.stop("limit_reached", "已达到用户设置的Token预算，进度已保存。", "token_budget")
                if state.get("stalled_action"):
                    return self.stop("partial", "相同工具动作持续没有产生新结果，进度已保存：" + state["stalled_action"], "repeated_action")
                if d.pending:
                    outcome = self._execute_pending(limits)
                    if outcome is not None:
                        return outcome
                    continue
                proposal = state.pop("completion_candidate", None)
                if proposal:
                    outcome = self._finish_answer(proposal)
                    if outcome is not None:
                        return outcome
                    continue
                decision = self.completion.decide(state)
                checkpoint = state.get("progress_checkpoint", {}).get("round", 0)
                if d.rounds and d.rounds % PROGRESS_CHECKPOINT_ROUNDS == 0 and checkpoint != d.rounds:
                    state["progress_checkpoint"] = {
                        "round": d.rounds, "tools_used": d.tool_count,
                        "remaining_rounds": remaining(limits["rounds"], d.rounds),
                        "remaining_seconds": max(0, round(d.deadline - time.time())) if d.deadline else None,
                        "instruction": "核对计划与实际进展，复用已有结果并优先完成验证；没有进展时调整方法。硬上限不会重置。",
                    }
                    self.store.event("progress_checkpoint", **state["progress_checkpoint"])
                    self.store.commit()
                final, recovery = decision == "answer", decision == "recover"
                if final and self.machine.state not in {"verify", "recover"}:
                    self.machine.transition("verify", "answer_ready")
                if exhausted(limits["rounds"], d.rounds):
                    if state["evidence_refs"] or state.get("partial_response"):
                        state["stop_reason"] = "model_budget"
                        return self._finish_answer(state.get("partial_response") or
                            "已达到模型轮次上限；已保存来源，请缩小问题后继续。", terminal=True)
                    return self.stop(
                        "limit_reached", "达到模型轮次上限", "model_budget"
                    )
                tool_definitions = self.runtime.definitions()
                context = self.context(recovery=recovery, final=final, tools=tool_definitions)
                response = self._call_model(context, tool_definitions)
                if cancellation_requested():
                    return self.cancel()
                if isinstance(response, dict):
                    return response
                calls = [asdict(call) for call in response.tool_calls or []]
                if not calls and self._serialized_tool_markup(response.content):
                    outcome = self._recover_serialized_tool_output()
                    if outcome is not None:
                        return outcome
                    continue
                if response.finish_reason == "length":
                    outcome = self._handle_length(response.content or "", calls)
                    if outcome is not None:
                        return outcome
                    continue
                if response.finish_reason not in {None, "stop", "tool_calls"}:
                    return self.stop(
                        "failed", "模型输出未正常结束：" + str(response.finish_reason)
                    )
                if not response.content and not calls:
                    outcome = self._handle_empty()
                    if outcome is not None:
                        return outcome
                    continue
                invalid = self._invalid_calls(calls, tool_definitions)
                if invalid:
                    outcome = self._recover_tool_protocol(invalid)
                    if outcome is not None:
                        return outcome
                    continue
                state.pop("response_issue", None)
                if calls:
                    state["phase"] = "explore"
                    state["stop_reason"] = None
                    if self.machine.state in {"verify", "recover"}:
                        self.machine.transition("execute", "continue_execution")
                if calls:
                    d.messages.append(
                        dict(
                            role="assistant",
                            content=response.content,
                            reasoning=response.reasoning or "",
                            tool_calls=[dict(call) for call in calls],
                        )
                    )
                    d.pending = [dict(call) for call in calls]
                    self.store.commit()
                    continue
                if not response.content:
                    continue
                outcome = self._finish_answer(response.content, reasoning=response.reasoning)
                if outcome is not None:
                    return outcome
        except KeyboardInterrupt:
            return self.cancel()
        except ContextLimitError as exc:
            return self.stop("failed", str(exc), "context_error")
        except Exception as exc:
            return self.fail(exc)

    @staticmethod
    def _serialized_tool_markup(text):
        return re.search(r"<[^>\n]{0,40}DSML[^>\n]{0,40}(?:calls|invoke|parameter)", text or "", re.I)

    def _finish_answer(self, answer_text, *, terminal=False, reasoning=None):
        """Validate every proposed final answer against the same evidence and task gates."""
        d = self.store.record
        state = d.run_runtime
        self.store.refresh_local_files()
        if terminal:
            # Exhausted recovery budgets must not deliver unchecked drafts or
            # launch another recovery/tool cycle.
            if (self._serialized_tool_markup(answer_text) or not self._answer_refs_valid(answer_text)
                    or self._unsupported_price_claims(answer_text)):
                answer_text = self._evidence_fallback('输出恢复已到上限，未能形成通过校验的完整回答。')
            elif state.get('partial_response'):
                endings = list(re.finditer(r'[。！？!?](?:[\]\)）"”]*)|\.(?=\s|$)', answer_text))
                answer_text = answer_text[:endings[-1].end()] if endings else ''
                answer_text += '\n模型输出达到上限，以上仅为已完成的部分；任务尚未形成完整说明。'
            # Trimming can remove a citation after a sentence. Recheck the
            # actual delivered text instead of starting another recovery cycle.
            if not self._answer_refs_valid(answer_text) or self._unsupported_price_claims(answer_text):
                answer_text = self._evidence_fallback('输出恢复已到上限，未能形成通过校验的完整回答。')
            answer_text = '[[PARTIAL]] ' + answer_text.lstrip().removeprefix('[[PARTIAL]]').strip()
        if self._serialized_tool_markup(answer_text):
            return self._recover_serialized_tool_output()
        # 使用项目验证器；未注册时由模型按风险选择验证方法。
        if (
            self._needs_verification or state.get("verification_required")
        ) and not terminal and self.development.should_auto_verify(state):
            outcome = self.development.inject_verification_test(self)
            if outcome != "skip":
                return outcome
        if not terminal and not answer_text.lstrip().startswith("[[PARTIAL]]"):
            issue = self.runtime.completion_issue()
            if issue:
                return self._request_repair("acceptance", issue, identity=issue)
        if self.machine.state in {"execute", "recover"}:
            self.machine.transition("verify", "model_answer")
        if not self._answer_refs_valid(answer_text):
            return self._recover_bad_refs(answer_text)
        unsupported_prices = self._unsupported_price_claims(answer_text)
        if unsupported_prices:
            return self._recover_unsupported_prices()
        if (state["recovery_counts"].get("evidence_claim") and not self._price_claims(answer_text)
                and not answer_text.lstrip().startswith("[[PARTIAL]]")):
            return self._price_gap()
        self.store.verify_artifacts(current_run_only=True)
        EvidenceBundle(self.store).verify()
        d.messages.append(
            dict(role="assistant", content=answer_text, reasoning=reasoning or "", tool_calls=None)
        )
        self.store.commit()
        partial = answer_text.lstrip().startswith("[[PARTIAL]]")
        answer = (
            answer_text.lstrip().removeprefix("[[PARTIAL]]").lstrip()
            if partial
            else answer_text
        )
        price_request = bool(
            re.search(
                r"价格|售价|多少钱|price|cost",
                str(state.get("objective") or ""),
                re.I,
            )
        )
        verified_price = (
            price_request
            and bool(self._price_amounts(answer_text))
            and not unsupported_prices
            and bool(SOURCE_REF.findall(answer_text))
        )
        if verified_price and not partial:
            state["stop_reason"] = "model_answer"
        verification_incomplete = bool(state.get("verification_required"))
        if verification_incomplete:
            state["stop_reason"] = "verification_failed"
        forced = state.get("stop_reason") not in {
            None,
            "model_answer",
            "finish_reason_length",
            "empty_response",
            "invalid_source_reference",
        }
        state["stop_reason"] = state.get("stop_reason") or (
            "evidence_gap" if partial else "model_answer"
        )
        return self.status(
            "partial"
            if partial or forced or verification_incomplete
            else "completed",
            answer,
        )

    def _execute_tool_call(self, call, approved=None):
        batch = next((message['tool_calls'] for message in reversed(self.store.record.messages)
                      if message.get('role') == 'assistant' and message.get('tool_calls')
                      and any(item.get('id') == call['id'] for item in message['tool_calls'])), [call])
        with self.runtime.submission_batch(batch):
            return self.runtime.execute(call, approved=approved)

    def _execute_pending(self, limits):
        """执行单个待处理工具调用并更新验证状态。"""
        d = self.store.record
        call = d.pending[0]
        prior = d.calls.get(call["id"])
        if prior and prior["state"] in {"unknown", "started"}:
            return self.status("interrupted", "存在不确定调用，禁止自动重放")
        if not prior and call["name"] != "read_job" and d.run_runtime.get("pending_question", {}).get("call_id") != call["id"]:
            if exhausted(limits["tools"], d.tool_count):
                if not d.run_runtime["evidence_refs"] and (
                    d.run_runtime["profile"] in {"direct_answer", "local_files"}
                    or d.run_runtime["usage"].get("search_queries", 0) == 0
                ):
                    return self.stop("limit_reached", "达到工具调用上限", "tool_budget")
                self.research.force_answer("tool_budget")
                self.close_pending("budget_exhausted")
                return None
            d.tool_count += 1
            self.store.commit()

        self.on_event("tool_start", dict(name=call["name"]))
        started = time.monotonic()
        result = self._execute_tool_call(call)
        elapsed_ms = round((time.monotonic() - started) * 1000)

        if "input_request" in result:
            question = result["input_request"]
            d.run_runtime["pending_question"] = {**question, "call_id": call["id"]}
            options = [f"{i}. {value}" for i, value in enumerate(question["options"], 1)]
            return self.status("awaiting_input", "\n".join([question["question"], *options]))
        if result.get("pending"):
            if cancellation_requested():
                return self.cancel()
            return self.status(
                "awaiting_confirmation", self.runtime.confirmation_message(call["name"])
            )
        self.store.event(
            "tool_end",
            tool_call_id=call["id"],
            tool=call["name"],
            ok=result.get("ok"),
            code=(result.get("error") or {}).get("code"),
            elapsed_ms=elapsed_ms,
            profile=d.run_runtime["profile"],
            phase=d.run_runtime["phase"],
        )
        if result.get("error") and result["error"]["code"] == "unknown_execution":
            if cancellation_requested():
                return self.cancel()
            return self.status("interrupted", result["error"]["message"])
        self.append_result(call, result)
        if cancellation_requested():
            return self.cancel()
        if self.research.should_force_answer():
            self.close_pending("research_complete")
        return None

    def _update_development_state(self, call, result):
        """Consume tool-owned facts for both direct and approved executions."""
        state = self.store.record.run_runtime
        for item in result.get("observations", []):
            if item["kind"] == "resource_changed":
                state["project_changed"] = True
                state["change_revision"] = state.get("change_revision", 0) + 1
                state.setdefault("changed_files", {})[item["path"]] = item.get("sha256")
                if self.development.requires_change_verification(
                    result, run_state=state
                ):
                    self._needs_verification = True
                    state["verification_required"] = True
                    state["verification_succeeded"] = False
            elif result.get("ok") and item["kind"] == "verification":
                passed = item["passed"]
                self._needs_verification = not passed
                state["verification_required"] = not passed
                state["verification_succeeded"] = passed
                state['verified_revision'] = state.get('change_revision', 0)
