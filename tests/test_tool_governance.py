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
        response(call("external_measure", {}, "premature")),
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
    assert names(0) == names(1)
    def system_prompt(turn):
        return "\n".join(m.content for m in model.contexts[turn] if m.role == "system")

    issue = system_prompt(1)
    assert "当前请求未提供的工具：external_measure" in issue and "请先用 discover_tools 查找" in issue
    assert "external_measure" in names(2)
    assert "当前请求未提供的工具：external_measure" not in system_prompt(2)
    assert "premature" not in store.record.calls
    assert store.record.calls["measure"]["result"]["data"]["value"] == 42
    assert runner.start("你好")["status"] == "completed"
    assert "external_measure" not in names(-1)


def test_duplicate_ids_get_distinct_guidance_and_batch_is_not_executed(env):
    _, _, store, runtime = env
    duplicate = call("discover_tools", {"query": "read_files"}, "repeated")
    model = FakeModel(response(duplicate, duplicate), Response(content="已停止"))
    Runner(model, runtime).start("你好")
    issue = "\n".join(m.content for m in model.contexts[1] if m.role == "system")
    assert "调用 ID 为空或重复" in issue
    assert "本批工具调用未执行，当前请求未提供" not in issue
    assert "repeated" not in store.record.calls


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
    # The deadline includes Windows process startup and module imports. Leave
    # enough room to enter the requested stage even during the full suite;
    # the work still must be killed before either delayed write can occur.
    timeout_seconds = 3
    work_delay = 5

    class Slow:
        def wait(self):
            if write and stage == "execute":
                script = f"import time; from pathlib import Path; time.sleep({work_delay}); Path({str(descendant)!r}).touch()"
                subprocess.Popen([sys.executable, "-c", script])
            started.touch()
            time.sleep(work_delay)
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
        timeout_seconds=timeout_seconds,
    )
    invocation = call("slow_tool", {}, "slow")
    before = time.monotonic()
    result = runtime.execute(invocation)
    assert started.exists() and time.monotonic() - before < timeout_seconds + 1.5
    assert result["error"]["code"] == code
    assert not result["error"]["retryable"]
    assert store.record.calls["slow"]["executed"] is (stage == "execute")
    assert runtime.execute(invocation)["error"]["code"] == code
    # Wait through the whole forbidden-write window, including descendants.
    time.sleep(work_delay + 0.2)
    assert not marker.exists() and not descendant.exists()


def test_successful_proposal_needs_no_extra_model_request(env):
    _, _, store, runtime = env
    model = FakeModel(
        response(call("attempt_completion", {"result": "结论"}, "finish"))
    )
    assert Runner(model, runtime).start("你好")["answer"] == "结论"
    assert len(model.contexts) == 1
    assert store.record.messages[-1]["content"] == "结论"


def test_completion_proposal_requires_execution_evidence(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(
        *[
            response(call("attempt_completion", {"result": "任务已完成"}, f"p{i}"))
            for i in range(4)
        ]
    )
    result = Runner(model, runtime).start("开始")
    assert result["status"] == "partial"
    assert store.record.run_runtime["stop_reason"] == "recovery_stalled"


def test_registered_proposal_runs_real_verifier(tmp_path):
    from genesisai.app.evals.plan_steps import complete_plan
    workspace, manager, _, store, runtime = development_env(tmp_path, planned=False)
    manager.begin_task('创建 main.py 并验证', new_task=True)
    manager.update_plan(expected_revision=0, upsert=[
        {'id': 'code', 'kind': 'step', 'text': '创建 main.py'},
        {'id': 'accept', 'kind': 'acceptance', 'text': '真实验证通过', 'requires_verification': True},
    ])

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
    runtime.development.register_verifier('python_syntax', lambda workspace, state, available: {
        'name': 'run_commands',
        'arguments': {'command': [sys.executable, '-m', 'py_compile', 'main.py'], 'verification': 'static'},
    })
    model = FakeModel(
        response(
            call(
                "editor",
                {"operation": "create", "path": "main.py", "content": "print(42)"},
                "write",
            )
        ),
        response(call("custom_submit", {}, "submit")),
        complete_plan(runtime),
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
    from genesisai.core.extensions.mcp.bridge import McpBridge, McpServerSpec

    _, _, store, runtime = env
    servers = [McpServerSpec("slow", "stdio", sys.executable, ("-c", "import sys,time; sys.stdin.readline(); time.sleep(30)"), request_timeouts={"first_response": 1})]
    bridge = McpBridge(tmp_path, runtime.registry, runtime.loader).configure({}, servers=servers)
    before = time.monotonic()
    result = runtime.execute(call("connect_mcp", {"server": "slow"}, "connect"))
    assert time.monotonic() - before < 3
    assert result["error"]["code"] == "unknown_execution"
    assert store.record.calls["connect"]["state"] == "unknown"
    assert not bridge.transports
