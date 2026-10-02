"""Tool Runtime 与 Runner 的按需加载、恢复和完整链路。"""

import dataclasses
import json

import pytest

from genesisai.shared.messages import Response
from genesisai.agent.runner import Runner
from genesisai.shared.security import Access
from genesisai.state.store import Store
from genesisai.runtime.registry import CORE_NAMES
from genesisai.runtime.tool_runtime import ToolRuntime
from test_acceptance import FakeModel, call, response


def make_runtime(tmp_path, **kwargs):
    inputs = tmp_path / "input"
    inputs.mkdir(exist_ok=True)
    (inputs / "note.md").write_text("RUNTIME_LOCAL", encoding="utf-8")
    work = tmp_path / "work"
    store = Store(work)
    output = tmp_path / "output"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(
        store,
        Access([inputs], output, work),
        providers=kwargs.pop("providers", []),
        confirm_writes=kwargs.pop("confirm_writes", False),
        confirm_search=kwargs.pop("confirm_search", False),
        **kwargs,
    )
    return inputs, output, store, runtime


def names(definitions):
    return [item["function"]["name"] for item in definitions]


def test_file_agent_discovers_describes_loads_and_executes(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    model = FakeModel(
        response(call("tool_search", {"query": "读取文件"}, "catalog")),
        response(call("tool_describe", {"name": "file_read"}, "describe")),
        response(call("tool_load", {"names": ["file_read"]}, "load")),
        response(call("file_read", {"path": "note.md"}, "read")),
        Response(content="done"),
    )

    result = Runner(model, runtime).start("读取 note.md")

    assert result["status"] == "completed"
    assert any(item["name"] == "file_read" for item in store.data["calls"]["catalog"]["result"]["data"]["tools"])
    assert all(names(item) == list(CORE_NAMES) for item in model.tool_definitions[:3])
    assert names(model.tool_definitions[3]) == [*CORE_NAMES, "file_read"]
    assert store.data["tool_runtime"]["active_tools"] == ["file_read"]
    assert any(value["kind"] == "file" for value in store.data["sources"].values())


def test_web_agent_preloads_searches_fetches_and_registers_source(tmp_path):
    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return [{"title": "Public", "url": "https://public.test/", "snippet": "candidate"}]

    class Pages:
        def fetch(self, url):
            return {"url": url, "title": "Public", "text": "RUNTIME_WEB", "truncated": False}

    _, _, store, runtime = make_runtime(tmp_path, providers=[Provider()])
    runtime.network = Pages()
    model = FakeModel(
        response(call("search_query", {"query": "public"}, "query")),
        response(call("search_fetch", {"url": "https://public.test/"}, "fetch")),
        Response(content="web done"),
    )

    result = Runner(model, runtime).start("搜索并读取公开资料")

    assert result["status"] == "completed"
    assert names(model.tool_definitions[0]) == [*CORE_NAMES, "search_query", "search_fetch"]
    assert len(model.contexts) == 3
    assert any(value["kind"] == "web" for value in store.data["sources"].values())


def test_tool_loaded_in_a_response_cannot_run_until_next_model_request(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    model = FakeModel(
        response(
            call("tool_load", {"names": ["file_read"]}, "load_same_round"),
            call("file_read", {"path": "note.md"}, "read_same_round"),
        )
    )

    result = Runner(model, runtime).start("尝试同轮加载和执行")

    assert result["status"] == "failed"
    assert "当前请求未提供" in result["answer"]
    assert "load_same_round" not in store.data["calls"]
    assert "read_same_round" not in store.data["calls"]


def test_active_tools_survive_resume_and_continue_in_followup(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    runtime.load_tools(["file_read"])
    store.data.update(status="interrupted", run_id="run_existing")
    store.save()

    loaded = Store(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[])
    assert restored.catalog.active_names == ["file_read"]
    assert names(restored.definitions()) == [*CORE_NAMES, "file_read"]

    restored.store.data["status"] = "completed"
    restored.store.save()
    model = FakeModel(Response(content="followup"))
    result = Runner(model, restored).start("继续追问")
    assert result["status"] == "completed"
    assert restored.catalog.active_names == ["file_read"]
    assert names(model.tool_definitions[0]) == [*CORE_NAMES, "file_read"]


def test_followup_reuses_previous_answer_and_active_web_tools(tmp_path):
    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return [{"title": "Watch", "url": "https://public.test/watch", "snippet": "candidate"}]

    class Pages:
        def fetch(self, url):
            return {"url": url, "title": "Watch", "text": "battery", "truncated": False}

    _, _, store, runtime = make_runtime(tmp_path, providers=[Provider()])
    runtime.network = Pages()

    first_model = FakeModel(
        response(call("tool_search", {"query": "网络"}, "follow_catalog")),
        response(call("tool_load", {"names": ["search_query", "search_fetch"]}, "follow_load")),
        response(call("search_query", {"query": "Apple Watch"}, "follow_query")),
        response(call("search_fetch", {"url": "https://public.test/watch"}, "follow_initial_fetch")),
        Response(content="Apple Watch 已发布"),
    )
    first = Runner(first_model, runtime)
    assert first.start("苹果发布会有什么产品")["status"] == "completed"

    model = FakeModel(
        response(call("search_fetch", {"url": "https://public.test/watch"}, "follow_fetch")),
        Response(content="它的优势是续航"),
    )
    result = Runner(model, runtime).start("这个产品的优势是什么")

    assert result["status"] == "completed"
    assert result["answer"] == "它的优势是续航"
    assert names(model.tool_definitions[0]) == [*CORE_NAMES, "search_query", "search_fetch"]
    assert [
        message.content
        for message in model.contexts[0]
        if message.role in {"user", "assistant"} and message.content is not None
    ][-3:] == [
        "苹果发布会有什么产品",
        "Apple Watch 已发布",
        "这个产品的优势是什么",
    ]
    assert [message.tool_call_id for message in model.contexts[0] if message.role == "tool"][-4:] == [
        "follow_catalog",
        "follow_load",
        "follow_query",
        "follow_initial_fetch",
    ]
    assert store.data["tool_runtime"]["active_tools"] == ["search_query", "search_fetch"]


def test_followup_keeps_file_tool_active_and_reads_next_section(tmp_path):
    inputs, _, store, runtime = make_runtime(tmp_path)
    (inputs / "note.md").write_text("第一部分\n第二部分：详细资料", encoding="utf-8")
    runtime.load_tools(["file_read"])

    first = FakeModel(
        response(call("file_read", {"path": "note.md", "offset": 0, "limit": 4}, "first_read")),
        Response(content="第一部分概括"),
    )
    assert Runner(first, runtime).start("读取并概括第一部分")["status"] == "completed"

    followup = FakeModel(
        response(call("file_read", {"path": "note.md", "offset": 4, "limit": 100}, "second_read")),
        Response(content="第二部分的详细解释"),
    )
    result = Runner(followup, runtime).start("把第二部分再详细解释")

    assert result["status"] == "completed"
    assert names(followup.tool_definitions[0]) == [*CORE_NAMES, "file_read"]
    assert store.data["calls"]["second_read"]["state"] == "succeeded"
    assert "第二部分" in store.data["calls"]["second_read"]["result"]["data"]["text"]
    assert [message.tool_call_id for message in followup.contexts[0] if message.role == "tool"][-1] == "first_read"


def test_three_followups_keep_session_context_and_reset_run_budget(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    prompts = ["第一问", "继续", "再说明", "最后一个问题"]
    answers = ["第一答", "第二答", "第三答", "第四答"]

    for prompt, answer in zip(prompts, answers):
        model = FakeModel(Response(content=answer, usage={"total_tokens": 1}))
        result = Runner(model, runtime, max_rounds=2, max_tools=3).start(prompt)
        assert result["status"] == "completed"
        assert store.data["rounds"] == 1
        assert store.data["tool_count"] == 0

    assert [message["content"] for message in store.data["messages"] if message["role"] != "tool"] == [
        value for pair in zip(prompts, answers) for value in pair
    ]
    assert len(store.data["history"]) == 3


def test_followup_fails_clearly_when_previous_turn_cannot_fit_context(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    store.data["messages"] = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "x" * 81000, "tool_calls": None},
    ]
    store.data["status"] = "completed"
    store.save()

    result = Runner(FakeModel(), runtime).start("基于上一轮继续")
    assert result["status"] == "failed"
    assert "无法同时保留当前追问和上一轮内容" in result["answer"]


def test_network_confirmation_uses_network_message(tmp_path):
    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return []

    _, _, _, runtime = make_runtime(tmp_path, providers=[Provider()], confirm_search=True)
    model = FakeModel(
        response(call("tool_load", {"names": ["search_query"]}, "load_network")),
        response(call("search_query", {"query": "Apple"}, "network_call")),
    )

    result = Runner(model, runtime).start("查询 Apple")

    assert result["status"] == "awaiting_confirmation"
    assert result["answer"] == "需要确认公开网络请求"
    assert runtime.confirmation_message("file_create") == "需要确认输出文件写入"
    assert runtime.confirmation_message("file_copy") == "需要确认输出文件写入"


def test_session_restore_keeps_messages_and_active_tools_for_followup(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    runtime.load_tools(["file_read"])
    assert Runner(FakeModel(Response(content="原回答")), runtime).start("原问题")["status"] == "completed"

    loaded = Store(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[])
    model = FakeModel(response(call("file_read", {"path": "note.md"}, "restored_read")), Response(content="追问回答"))
    result = Runner(model, restored).start("继续解释它")

    assert result["status"] == "completed"
    assert restored.catalog.active_names == ["file_read"]
    assert [message.content for message in model.contexts[0] if message.role in {"user", "assistant"}][-3:] == [
        "原问题",
        "原回答",
        "继续解释它",
    ]


def test_permission_modes_are_independent_and_session_local(tmp_path):
    _, _, _, runtime = make_runtime(tmp_path, confirm_writes=True, confirm_search=True)
    assert runtime.permission_mode == "ASK"

    runtime.set_permission("network", "allow")
    assert runtime.confirm_search is False and runtime.confirm_writes is True
    assert runtime.permission_mode == "MIXED"

    runtime.set_permission("writes", "allow")
    assert runtime.confirm_search is False and runtime.confirm_writes is False
    assert runtime.permission_mode == "MIXED"

    runtime.set_permission("shell", "allow")
    assert runtime.permission_mode == "ALLOW"

    runtime.set_permission("network", "ask")
    assert runtime.confirm_search is True and runtime.confirm_writes is False
    assert runtime.permission_mode == "MIXED"

    with pytest.raises(ValueError, match="权限类型"):
        runtime.set_permission("database", "allow")
    with pytest.raises(ValueError, match="权限模式"):
        runtime.set_permission("network", "always")


@pytest.mark.parametrize(
    "confirm_writes,confirm_search,write_pending,network_pending",
    [
        (True, True, True, True),
        (True, False, True, False),
        (False, True, False, True),
        (False, False, False, False),
    ],
)
def test_permission_matrix_is_independent(
    tmp_path, confirm_writes, confirm_search, write_pending, network_pending
):
    _, _, _, runtime = make_runtime(
        tmp_path,
        confirm_writes=confirm_writes,
        confirm_search=confirm_search,
    )

    assert runtime.policy.requires_confirmation(runtime.registry.get("file_create").spec) is write_pending
    assert runtime.policy.requires_confirmation(runtime.registry.get("search_query").spec) is network_pending


def test_permission_switch_does_not_unfreeze_prepared_call(tmp_path):
    inputs, output, store, runtime = make_runtime(tmp_path, confirm_writes=True)
    model = FakeModel(
        response(call("tool_load", {"names": ["file_copy"]}, "load_frozen_copy")),
        response(call("file_copy", {"source": "note.md", "path": "copy.md"}, "frozen_copy")),
        Response(content="handled"),
    )
    runner = Runner(model, runtime)
    assert runner.start("复制文件")["status"] == "awaiting_confirmation"

    runtime.set_permission("writes", "allow")
    (inputs / "note.md").write_text("CHANGED", encoding="utf-8")
    result = runner.confirm(True)

    assert result["status"] == "completed"
    assert not (output / "copy.md").exists()
    assert store.data["calls"]["frozen_copy"]["result"]["error"]["code"] == "confirmation_invalid"


def test_missing_restored_tool_is_unavailable_and_not_offered(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    store.data["tool_runtime"]["active_tools"] = ["removed_tool"]
    store.save()

    loaded = Store(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[])
    result = restored.execute(call("removed_tool", {}, "removed"))

    assert "removed_tool" not in names(restored.definitions())
    assert result["error"]["code"] == "tool_unavailable"
    assert any(item["name"] == "removed_tool" for item in restored.snapshot()["disabled"])


def test_confirmation_restart_restores_active_tool(tmp_path):
    _, output, store, runtime = make_runtime(tmp_path, confirm_writes=True)
    model = FakeModel(
        response(call("tool_load", {"names": ["file_create"]}, "load_write")),
        response(call("file_create", {"path": "result.md", "content": "ok", "source_refs": []}, "write")),
    )
    runner = Runner(model, runtime)
    assert runner.start("创建文件")["status"] == "awaiting_confirmation"

    loaded = Store(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[], confirm_writes=True)
    assert restored.catalog.active_names == ["file_create"]

    result = Runner(FakeModel(Response(content="done")), restored).confirm(True)
    assert result["status"] == "completed"
    assert (output / "result.md").read_text(encoding="utf-8") == "ok"


def test_disabled_tool_cannot_execute_after_confirmation(tmp_path, monkeypatch):
    _, output, store, runtime = make_runtime(tmp_path, confirm_writes=True)
    model = FakeModel(
        response(call("tool_load", {"names": ["file_create"]}, "load_write_disabled")),
        response(call("file_create", {"path": "blocked.md", "content": "x", "source_refs": []}, "write_disabled")),
        Response(content="handled"),
    )
    runner = Runner(model, runtime)
    assert runner.start("创建文件")["status"] == "awaiting_confirmation"
    def disable_on_refresh():
        runtime.registry._items["file_create"] = dataclasses.replace(
            runtime.registry.get("file_create"), enabled=False
        )

    monkeypatch.setattr(runtime.registry, "refresh", disable_on_refresh)

    result = runner.confirm(True)

    assert result["status"] == "completed"
    assert not (output / "blocked.md").exists()
    assert store.data["calls"]["write_disabled"]["result"]["error"]["code"] == "tool_unavailable"


def test_final_ledger_failure_marks_completed_write_unknown(tmp_path, monkeypatch):
    _, output, store, runtime = make_runtime(tmp_path)
    runtime.load_tools(["file_create"])

    def fail_finish(*args, **kwargs):
        raise OSError("fixture ledger failure")

    monkeypatch.setattr(runtime.ledger, "finish", fail_finish)
    result = runtime.execute(
        call("file_create", {"path": "unknown.md", "content": "written", "source_refs": []}, "unknown_write")
    )

    assert result["error"]["code"] == "unknown_execution"
    assert (output / "unknown.md").exists()
    assert store.data["calls"]["unknown_write"]["state"] == "unknown"
    with pytest.raises(ValueError, match="不确定"):
        Runner(FakeModel(Response(content="unsafe")), runtime).start("新任务")


def test_cancel_acknowledges_unknown_without_replaying_it(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    uncertain = call("file_create", {"path": "unknown.md", "content": "x", "source_refs": []}, "unknown")
    store.data["calls"]["unknown"] = {
        "call": uncertain,
        "state": "unknown",
        "result": runtime.error(uncertain, "unknown_execution", "结果不确定"),
    }
    store.data["status"] = "interrupted"
    store.save()
    runner = Runner(FakeModel(Response(content="继续完成")), runtime)

    with pytest.raises(ValueError, match="处理确认或 /cancel"):
        runner.start("直接继续")

    assert runner.cancel()["status"] == "cancelled"
    assert runner.start("确认后继续")["status"] == "completed"
    assert store.data["calls"]["unknown"]["state"] == "unknown"
    assert store.data["calls"]["unknown"]["acknowledged"] is True


@pytest.mark.parametrize(
    "tool_runtime",
    [
        [],
        {},
        {"active_tools": "file_read"},
        {"active_tools": ["file_read", "file_read"]},
        {"active_tools": [""]},
        {"active_tools": [str(index) for index in range(9)]},
        {"active_tools": [], "implementation": "unsafe"},
    ],
)
def test_invalid_session_tool_runtime_is_rejected_without_rewrite(tmp_path, tool_runtime):
    store = Store(tmp_path / "work")
    data = dict(store.data)
    data["tool_runtime"] = tool_runtime
    store.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = store.path.read_bytes()

    with pytest.raises(ValueError, match="tool_runtime|active_tools"):
        Store(store.root, store.id)

    assert store.path.read_bytes() == before

