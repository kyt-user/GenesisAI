"""Regressions at the model, tool execution, and completion boundaries."""

import json
import subprocess
import sys
import time

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.shared.messages import Response
from test_acceptance import FakeModel, call, env, response
from test_development_continuation import development_env


def register(runtime, name, implementation, **policy):
    descriptor = ToolDescriptor(
        name,
        True,
        "kernel",
        name,
        runtime.store.workspace,
        ToolSpec(parameters=object_schema([]), **policy),
    )
    runtime.register_tool(descriptor, implementation)


def test_discover_new_tool_mid_run_and_reset_next_task(env):
    _, _, store, runtime = env

    class Custom:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            return {"value": 42}

    register(runtime, "external_measure", Custom(), permission="read")
    model = FakeModel(
        response(call("discover_tools", {"query": "external_measure"}, "discover")),
        response(call("external_measure", {}, "measure")),
        Response(content="42"),
        Response(content="hi"),
    )
    runner = Runner(model, runtime)
    assert runner.start("检查仓库代码")["answer"] == "42"
    names = lambda turn: {t["function"]["name"] for t in model.tool_definitions[turn]}
    assert "external_measure" not in names(0)
    assert "xlsx_create" not in names(0)
    assert "external_measure" in names(1)
    assert store.record.calls["measure"]["result"]["data"]["value"] == 42
    assert runner.start("你好")["status"] == "completed"
    assert "external_measure" not in names(-1)


def test_truncated_result_is_bounded_and_exactly_readable(env):
    _, _, store, runtime = env

    class Verbose:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            return {"text": '工具输出"\\\n' * 400}

    register(
        runtime, "verbose_tool", Verbose(), permission="read", max_output_chars=256
    )
    model = FakeModel(
        response(call("verbose_tool", {}, "big")), Response(content="done")
    )
    assert Runner(model, runtime).start("verbose_tool")["status"] == "completed"
    tool_message = next(m for m in model.contexts[1] if m.role == "tool")
    assert len(tool_message.content) <= 256
    preview = json.loads(tool_message.content)
    assert preview["truncated"] and preview["chars"] > 256
    assert "memory_read" in {t["function"]["name"] for t in model.tool_definitions[1]}
    pieces, offset = [], 0
    while offset is not None:
        page = runtime.execute(
            call(
                "memory_read",
                {"ref": preview["ref"], "offset": offset, "limit": 700},
                f"p{offset}",
            )
        )
        assert page["ok"]
        pieces.append(page["data"]["text"])
        offset = page["data"]["next_offset"]
    assert json.loads("".join(pieces)) == store.record.calls["big"]["result"]


@pytest.mark.parametrize(
    "stage,write,code",
    [
        ("prepare", True, "tool_timeout"),
        ("execute", False, "tool_timeout"),
        ("execute", True, "unknown_execution"),
    ],
)
def test_tool_deadline_kills_work_and_prevents_late_write(
    env, tmp_path, stage, write, code
):
    _, _, store, runtime = env
    marker = tmp_path / "late"
    descendant = tmp_path / "late-child"
    started = tmp_path / "entered"

    class Slow:
        def wait(self):
            if write and stage == "execute":
                script = f"import time; from pathlib import Path; time.sleep(1.9); Path({str(descendant)!r}).touch()"
                subprocess.Popen([sys.executable, "-c", script])
            started.touch()
            time.sleep(2.0)
            marker.touch()

        def prepare(self, context, args):
            if stage == "prepare":
                self.wait()
            return {"target": "test-resource"}

        def execute(self, context, args):
            if stage == "execute":
                self.wait()
            return {}

    register(
        runtime,
        "slow_tool",
        Slow(),
        permission="write" if write else "read",
        side_effect=write,
        timeout_seconds=1,
    )
    invocation = call("slow_tool", {}, "slow")
    before = time.monotonic()
    result = runtime.execute(invocation)
    assert started.exists() and time.monotonic() - before < 1.9
    assert result["error"]["code"] == code
    assert not result["error"]["retryable"]
    assert store.record.calls["slow"]["executed"] is (stage == "execute")
    assert runtime.execute(invocation)["error"]["code"] == code
    time.sleep(1.4)
    assert not marker.exists() and not descendant.exists()


def test_successful_proposal_needs_no_extra_model_request(env):
    _, _, store, runtime = env
    model = FakeModel(
        response(call("attempt_completion", {"result": "结论"}, "finish"))
    )
    assert Runner(model, runtime).start("你好")["answer"] == "结论"
    assert len(model.contexts) == 1
    assert store.record.messages[-1]["content"] == "结论"


def test_completion_proposal_cannot_skip_required_changes(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(
        *[
            response(call("attempt_completion", {"result": "任务已完成"}, f"p{i}"))
            for i in range(2)
        ]
    )
    result = Runner(model, runtime).start("开始")
    assert result["status"] == "partial"
    assert store.record.run_runtime["stop_reason"] == "tool_required"


def test_registered_proposal_runs_real_verifier(tmp_path):
    workspace, _, _, store, runtime = development_env(tmp_path)

    class Submit:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            return {"answer": "已经写入源码"}

    register(
        runtime,
        "custom_submit",
        Submit(),
        permission="core",
        completion_field="answer",
        routes=("coding",),
    )
    model = FakeModel(
        response(
            call(
                "editor",
                {"operation": "create", "path": "main.py", "content": "print(42)"},
                "write",
            )
        ),
        response(call("custom_submit", {}, "submit")),
        Response(content="验证通过"),
    )
    result = Runner(model, runtime).start("开始")
    assert result["status"] == "completed"
    assert (workspace / "main.py").exists() and store.record.run_runtime[
        "verification_succeeded"
    ]
    assert any(
        a["tool"] == "run_commands" for a in store.record.run_runtime["tool_activity"]
    )


def test_stalled_stdio_mcp_closes_owned_server(env, tmp_path):
    from dataclasses import replace
    from genesisai.core.extensions.mcp.bridge import McpBridge

    _, _, store, runtime = env
    config = {
        "servers": {
            "slow": {
                "command": sys.executable,
                "args": ["-c", "import sys,time; sys.stdin.readline(); time.sleep(30)"],
            }
        }
    }
    (tmp_path / "mcp.json").write_text(json.dumps(config), encoding="utf8")
    bridge = McpBridge(tmp_path, runtime.registry, runtime.loader).configure({})
    descriptor = runtime.registry.get("connect_mcp")
    runtime.registry.register(
        replace(descriptor, spec=replace(descriptor.spec, timeout_seconds=1))
    )
    before = time.monotonic()
    result = runtime.execute(call("connect_mcp", {"server": "slow"}, "connect"))
    assert time.monotonic() - before < 3
    assert result["error"]["code"] == "unknown_execution"
    assert store.record.calls["connect"]["state"] == "unknown"
    assert not bridge.transports
