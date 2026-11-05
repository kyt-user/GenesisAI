"""任务档位与有界模型上下文。"""

from __future__ import annotations

from dataclasses import asdict, replace

from genesisai.core.prompt.compaction import (
    estimate_tokens,
    fingerprint,
    message_units,
    project,
    project_images,
)
from genesisai.shared.context_config import ContextConfig
from genesisai.core.prompt.providers import ContextProviders
from genesisai.core.prompt.context_sources import register_builtins
from genesisai.core.persistence.database import encode
from genesisai.shared.ids import uid
import time
from genesisai.shared.messages import (
    Message,
    ToolCall,
    content_text,
    image_parts,
    image_tokens,
)


class ContextBudgeter:
    """Persist a working-context projection separately from the full transcript."""

    def __init__(self, store, composer, *, config=None):
        self.store, self.composer = store, composer
        self.config = ContextConfig.parse(config)
        self.summarize = None
        self.tools = lambda: []
        self._last_estimate = None
        self.providers = ContextProviders()
        register_builtins(self.providers)
        self._prefix_length = 1

    def configure(
        self,
        config=None,
        *,
        output_tokens=None,
        summarize=None,
        tools=None,
        estimation_key=None,
    ):
        self.config = ContextConfig.parse(config)
        if output_tokens is not None:
            self.config = replace(
                self.config,
                output_reserve_tokens=max(
                    self.config.output_reserve_tokens, int(output_tokens)
                ),
            )
        if self.config.input_limit <= 0:
            raise ValueError("No input budget remains after reserving output tokens")
        self.summarize = summarize
        if tools is not None:
            self.tools = tools
        if (
            estimation_key is not None
            and getattr(self.store.record, "context_model") != estimation_key
        ):
            self.store.record.context_model = estimation_key
            self.store.record.context_estimate_factor = 1.0
            self._last_estimate = None
            self.store.commit()

    def working_messages(self):
        raw = self.store.history_projection()
        checkpoint = getattr(self.store.record, "context_checkpoint")
        if checkpoint:
            count = checkpoint["source_count"]
            if (
                count <= len(raw)
                and fingerprint(self.store.record.messages[:count])
                == checkpoint["source_hash"]
            ):
                return project_images(
                    checkpoint["messages"] + raw[count:],
                    self.config.max_images,
                    self.config.max_request_image_bytes,
                )
            self.store.get_logger().record(
                "context_checkpoint_invalidated", source_count=count
            )
            self.store.record.context_checkpoint = None
            self.store.commit()
        return project_images(
            raw, self.config.max_images, self.config.max_request_image_bytes
        )

    def build(
        self, profile: str, *, recovery: bool = False, final: bool = False
    ) -> list[Message]:
        """构建当前轮次的完整模型上下文消息列表。"""
        state = getattr(self.store.record, "run_runtime") or {}
        composed = self.composer.compose(profile, state, recovery=recovery, final=final)
        state["prompt_names"] = composed.names
        state["prompt_hashes"] = composed.hashes
        blocks, manifest = self.providers.build(
            dict(
                session=self.store,
                runtime=getattr(self, "runtime", None),
                profile=profile,
            )
        )
        manifest = [
            dict(
                provider="prompt",
                source=source,
                scope="application",
                role="system",
                trust="policy",
                tokens=estimate_tokens(content),
                budget=self.config.input_limit,
                included=True,
                hash=fingerprint(content),
            )
            for source, content in [
                ("policy_prefix", composed.stable),
                ("protocol_and_state", composed.dynamic),
            ]
        ] + manifest
        prefix = [
            Message(role="system", content=composed.stable),
            Message(role="system", content=composed.dynamic),
            *blocks,
        ]
        self._prefix_length = len(prefix)
        messages = [*prefix, *self._messages(self.working_messages())]
        if recovery and state.get("partial_response"):
            messages.append(
                Message(
                    role="user",
                    content="Continue the interrupted answer using recorded evidence:\n"
                    + state["partial_response"][-8000:],
                )
            )
        state["context_report"] = {
            "manifest": manifest,
            "system_sections": {
                "policy": len(composed.stable),
                "protocol": len(composed.dynamic),
            },
        }
        return messages

    @staticmethod
    def _messages(values):
        return [
            Message(
                role=m["role"],
                content=m.get("content"),
                tool_calls=[ToolCall(**call) for call in m.get("tool_calls") or []]
                or None,
                tool_call_id=m.get("tool_call_id"),
                metadata=m.get("metadata"),
            )
            for m in values
        ]

    @staticmethod
    def request_tokens(messages, tools):
        # Count the serialized payload including tool arguments, schemas and system text.
        payload = [
            {
                k: v
                for k, v in asdict(m).items()
                if v is not None and k not in {"metadata", "reasoning"}
            }
            for m in messages
        ]
        return estimate_tokens({"messages": payload, "tools": tools})

    def note_usage(self, actual_input):
        if type(actual_input) is int and actual_input > 0 and self._last_estimate:
            factor = min(4.0, max(1.0, actual_input / self._last_estimate))
            self.store.record.context_estimate_factor = max(
                factor, getattr(self.store.record, "context_estimate_factor", 1.0)
            )
            self.store.commit()

    def prepare(self, messages, tools, *, mode="auto"):
        config = self.config
        factor = getattr(self.store.record, "context_estimate_factor", 1.0)
        limit = int(config.input_limit / factor)
        trigger = int(limit * 0.90)
        before = self.request_tokens(messages, tools)
        report = {
            "mode": mode,
            "folded": False,
            "tokens_before": before,
            "context_window": config.context_window,
            "history_tokens": config.history_tokens,
            "output_reserve_tokens": config.output_reserve_tokens,
            "safety_margin_tokens": config.safety_margin_tokens,
            "input_limit": limit,
            "trigger_tokens": trigger,
            "estimate_factor": factor,
        }
        raw = self.store.record.messages
        working = self.working_messages()
        # Build() adds system text and possibly one recovery reminder outside the transcript.
        prefix = messages[: self._prefix_length]
        suffix = messages[self._prefix_length + len(working) :]
        overhead = self.request_tokens([*prefix, *suffix], tools)
        latest_user = next((m for m in reversed(working) if m["role"] == "user"), None)
        current_tokens = estimate_tokens([latest_user]) if latest_user else 0
        working_tokens = estimate_tokens(working)
        if (
            before >= trigger
            or working_tokens - current_tokens > config.history_tokens
            or mode != "auto"
        ):
            target = (
                int(trigger * 0.70)
                if mode == "auto"
                else min(
                    int(trigger * 0.70),
                    overhead + max(1, working_tokens // 2),
                )
            )
            if mode == "overflow_recovery":
                target = min(target, int(before * 0.5))
            else:
                # The latest request may use the full window. Only older material
                # competes for the working-history allowance.
                target = min(
                    limit,
                    max(
                        target,
                        overhead
                        + current_tokens
                        + min(config.summary_max_tokens, config.history_tokens),
                    ),
                )
            available = min(target - overhead, current_tokens + config.history_tokens)
            if available <= 0:
                raise ValueError(
                    "System prompt, tools and recovery input exceed the context budget"
                )
            callback = (
                self.summarize
                if config.strategy == "agentic" and mode != "overflow_recovery"
                else None
            )
            try:
                projected, details = project(
                    working, available, config.preserve_recent_tokens, callback
                )
            except ValueError:
                if mode != "manual" or before >= trigger:
                    raise
                projected, details = (
                    working,
                    {"folded": False, "reason": "necessary_context_already_small"},
                )
            candidate = [*prefix, *self._messages(projected), *suffix]
            after = self.request_tokens(candidate, tools)
            if after > limit or (mode == "overflow_recovery" and after >= before):
                raise ValueError(
                    "Context remains too large after compaction; no safe retry is possible"
                )
            report.update(
                {k: v for k, v in details.items() if k != "facts"},
                target_tokens=target,
                tokens_after=after,
            )
            if details["folded"]:
                checkpoint = {
                    "source_count": len(raw),
                    "source_hash": fingerprint(raw),
                    "messages": projected,
                }
                self.store.record.context_checkpoint = checkpoint
                report.update(source_hash=checkpoint["source_hash"])
                self.store.record.run_runtime["context_summary"] = report
                self.store.get_logger().record("context_compaction", **report)
            messages = candidate
        after = self.request_tokens(messages, tools)
        if after > limit:
            raise ValueError("Request exceeds the configured context input limit")
        message_units([asdict(m) for m in messages[self._prefix_length :]])
        report["tokens_after"] = after
        self._last_estimate = after
        roles = {role: 0 for role in ("user", "assistant", "tool", "other")}
        for message in messages[1:]:
            roles[message.role if message.role in roles else "other"] += len(
                content_text(message.content)
            )
        images = [part for message in messages for part in image_parts(message.content)]
        prior = self.store.record.run_runtime.get("context_report", {})
        self.store.record.run_runtime["context_report"] = {
            **report,
            "manifest": prior.get("manifest", []),
            "system_sections": prior.get("system_sections", {}),
            "system_chars": len(messages[0].content or ""),
            "message_role_chars": roles,
            "message_chars": sum(roles.values()),
            "total_chars": len(messages[0].content or "") + sum(roles.values()),
            "tool_definition_tokens": estimate_tokens(tools),
            "image_count": len(images),
            "image_tokens": sum(image_tokens(p) for p in images),
            "image_bytes": sum(p["byte_size"] for p in images),
        }
        with self.store.database.transaction() as db:
            db.execute(
                "INSERT INTO context_manifests VALUES(?,?,?,?)",
                (
                    uid("context"),
                    self.store.id,
                    encode(self.store.record.run_runtime["context_report"]),
                    time.time(),
                ),
            )
        self.store.commit()
        return messages

    def compact(self, *, force=False, mode="auto"):
        if getattr(self.store.record, "pending"):
            raise ValueError("Resolve pending tool approvals before compacting context")
        state = self.store.record.run_runtime
        messages = self.build(state["profile"])
        self.prepare(messages, self.tools(), mode="manual" if force else mode)
        return state["context_report"]
