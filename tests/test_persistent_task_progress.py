import json

from genesisai.agent.progress import ProgressTracker
from test_capability_lifecycle import capability_runtime


def call(path):
    return {"name": "read_files", "arguments": json.dumps({"path": path})}


def test_different_failed_actions_do_not_exhaust_global_task():
    tracker = ProgressTracker({})
    error = {"ok": False, "error": {"code": "not_found"}}
    assert all(tracker.observe(call(str(index)), error) for index in range(20))
    assert all(tracker.observe(call("missing"), error) for _ in range(3))
    assert not tracker.observe(call("missing"), error)


def test_only_new_observed_information_resets_retry_epoch():
    tracker = ProgressTracker({})
    result = {"ok": True, "data": {"text": "current", "source_ref": "src_one"}}
    assert tracker.observe(call("file"), result)
    for _ in range(3):
        assert tracker.retry("invalid_protocol", "same_call")
    result["data"]["source_ref"] = "src_two"
    tracker.observe(call("file"), result)
    assert not tracker.retry("invalid_protocol", "same_call")
    result["data"]["text"] = "changed on disk"
    tracker.observe(call("file"), result)
    assert tracker.retry("invalid_protocol", "same_call")


def test_checkpoint_retains_repeated_action_detection():
    state = {}
    tracker = ProgressTracker(state)
    for _ in range(3):
        assert tracker.retry("empty_response", "empty")
    restored = ProgressTracker(json.loads(json.dumps(state)))
    assert not restored.retry("empty_response", "empty")


def test_provider_reasoning_state_survives_tool_turn_and_session_restore(capability_runtime):
    from genesisai.agent.loop import Runner
    from genesisai.core.persistence.session import Session
    from genesisai.model.providers.deepseek import DeepSeekClient
    from genesisai.shared.messages import Message, Response, ToolCall
    from test_p11_agent_runtime import FakeModel
    runtime = capability_runtime
    (runtime.store.workspace / "note.txt").write_text("fixture", encoding="utf-8")
    model = FakeModel(Response(tool_calls=[ToolCall("read", "read_files", '{"path":"note.txt"}')], reasoning="opaque provider state"),
                      Response(content="已读。", reasoning="opaque final state"))
    assert Runner(model, runtime).start("读取 note.txt")["status"] == "completed"
    restored = Session(runtime.store.root, runtime.store.id)
    messages = runtime.context_budgeter._messages(restored.record.messages)
    client = object.__new__(DeepSeekClient)
    wire = client._request_messages([*messages, Message(role="assistant", content="host verification")])
    assert next(m for m in wire if m.get("tool_calls"))["reasoning_content"] == "opaque provider state"
    assert wire[-2]["reasoning_content"] == "opaque final state"
    assert wire[-1]["reasoning_content"] == ""


def test_capability_test_verifies_only_matching_changed_sources(capability_runtime):
    from test_capability_lifecycle import created, invoke, reference, tool_package
    from genesisai.shared.ids import sha
    runtime = capability_runtime
    item = created(runtime, tool_package())
    source = runtime.store.workspace / "authoring.py"
    asset = item["package"]["files"]["tool.py"]
    source.write_bytes(runtime.capabilities.store.assets.resolve(asset).read_bytes())
    runtime.store.record.run_runtime["changed_files"] = {str(source): sha(source)}
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["verification_current"]
    assert tested["observations"][0]["passed"]
    source.write_text("unrelated change", encoding="utf-8")
    _, stale = invoke(runtime, "test_capability", reference(item))
    assert stale["data"]["report"]["passed"]
    assert not stale["data"]["verification_current"] and not stale["observations"]


def test_running_background_poll_does_not_claim_progress_or_block():
    state = {}
    tracker = ProgressTracker(state)
    pending = {"name": "read_job", "arguments": "{}"}
    assert all(tracker.observe(pending, {"ok": True, "data": {"status": "running"}}) for _ in range(20))
    assert tracker.data["epoch"] == 0


def test_runner_continues_past_old_round_and_tool_budgets(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response, ToolCall
    from test_development_continuation import development_env
    from test_p11_agent_runtime import FakeModel

    _, _, _, store, runtime = development_env(tmp_path)
    calls = [ToolCall(f"call_{i}", "read_files", json.dumps({"path": f"part_{i}.txt"})) for i in range(183)]
    responses = [Response(tool_calls=calls[i:i + 3], finish_reason="tool_calls") for i in range(0, len(calls), 3)]
    model = FakeModel(*responses, Response(content="所有分段已读取。"))
    monkeypatch.setattr(runtime, "execute", lambda c, approved=None: {"ok": True, "data": {"text": c["arguments"]}, "observations": []})
    result = Runner(model, runtime).start("逐段读取本地资料")
    assert result["status"] == "completed", result
    assert store.record.rounds == 62
    assert store.record.tool_count == 183
    assert store.record.deadline == 0
    assert store.record.limits == {"rounds": None, "tools": None, "total_tokens": None}
    assert store.record.run_runtime["progress_checkpoint"]["round"] == 60
    runtime.close()


def test_truncated_tool_arguments_are_not_executed_and_tools_remain_available(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response, ToolCall
    from test_development_continuation import development_env
    from test_p11_agent_runtime import FakeModel

    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(
        Response(tool_calls=[ToolCall("broken", "read_files", '{"path":')], finish_reason="length"),
        Response(tool_calls=[ToolCall("valid", "read_files", '{"path":"notes.txt"}')], finish_reason="tool_calls"),
        Response(content="已读取。"),
    )
    executed = []
    def execute(c, approved=None):
        executed.append(c["id"])
        return {"ok": True, "data": {"text": "notes"}, "observations": []}
    monkeypatch.setattr(runtime, "execute", execute)
    result = Runner(model, runtime).start("读取本地资料")
    assert result["status"] == "completed", result
    assert executed == ["valid"]
    assert "read_files" in {t["function"]["name"] for t in model.tools[1]}
    assert "当前请求禁止调用任何工具" not in str(model.contexts[1])
    runtime.close()


def test_explicit_token_budget_stops_before_another_action(tmp_path):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response, ToolCall
    from test_development_continuation import development_env
    from test_p11_agent_runtime import FakeModel

    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(Response(tool_calls=[ToolCall("unused", "read_files", '{"path":"notes.txt"}')],
                               finish_reason="tool_calls", usage={"total_tokens": 20}))
    result = Runner(model, runtime, token_budget=20).start("读取本地资料")
    assert result["status"] == "limit_reached"
    assert store.record.run_runtime["stop_reason"] == "token_budget"
    assert store.record.tool_count == 0
    runtime.close()


def test_builder_completion_uses_current_host_state(capability_runtime):
    from test_capability_lifecycle import tool_package, created, invoke, reference, accept
    runtime = capability_runtime
    state = runtime.store.record.run_runtime
    state.update(protocols=["capability_builder"], execution_requested=True, capability_goal="registered")
    assert "草稿" in runtime.completion_issue()
    item = created(runtime, tool_package())
    assert "draft" in runtime.completion_issue()
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"]
    assert "tested" in runtime.completion_issue()
    state["capability_goal"] = "tested"
    assert runtime.completion_issue() is None
    state["capability_goal"] = "registered"
    accept(runtime, item)
    assert runtime.completion_issue() is None
    runtime.capabilities.store.disable(item["id"])
    assert runtime.completion_issue() is not None


def test_short_builder_followup_keeps_goal_and_read_only_constraint():
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    selector = TaskProtocolSelector()
    previous = selector.route("创建 MCP 并注册到能力库")
    route = selector.route("那就不保留了", previous=previous)
    assert route["followup"] and route["execution_requested"]
    assert route["capability_goal"] == "registered"
    assert "capability_builder" in route["protocols"]
    previous = selector.route("创建 MCP，先只讨论结构，暂不改代码")
    route = selector.route("那就不保留了", previous=previous)
    assert route["constraints"]["read_only"]
    assert not route["execution_requested"]


def test_no_change_conclusion_needs_observation_but_no_forced_write(capability_runtime):
    runtime = capability_runtime
    state = runtime.store.record.run_runtime
    state.update(protocols=["coding"], execution_requested=True, tool_activity=[])
    assert runtime.completion_issue() is not None
    state["tool_activity"].append({"tool": "read_files", "ok": True})
    assert runtime.completion_issue() is None


def test_job_waiting_does_not_consume_explicit_tool_allowance(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response, ToolCall
    from test_development_continuation import development_env
    from test_p11_agent_runtime import FakeModel
    _, _, _, store, runtime = development_env(tmp_path)
    responses = [Response(tool_calls=[ToolCall("read", "read_files", '{"path":"notes.txt"}')])]
    responses.extend(Response(tool_calls=[ToolCall(f"poll_{i}", "read_job", '{"job_id":"job_fixture"}')]) for i in range(6))
    responses.append(Response(content="[[PARTIAL]] 后台作业仍在运行。"))
    def execute(call, approved=None):
        return {"ok": True, "data": {"status": "running"} if call["name"] == "read_job" else {"text": "notes"}, "observations": []}
    monkeypatch.setattr(runtime, "execute", execute)
    result = Runner(FakeModel(*responses), runtime, max_tools=1).start("读取本地资料并查看后台作业")
    assert result["status"] == "partial", result
    assert store.record.tool_count == 1
    assert len(store.record.run_runtime["tool_activity"]) == 7
    runtime.close()
