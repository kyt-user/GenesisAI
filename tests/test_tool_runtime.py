"""Tool Runtime 与 Runner 的全量下发、恢复和完整链路。"""

import dataclasses
import json

import pytest

from genesisai.shared.messages import Response
from genesisai.agent.loop import Runner
from genesisai.shared.security import Access
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from test_acceptance import FakeModel, call, response


def make_runtime(tmp_path, **kwargs):
    inputs = tmp_path / "input"
    inputs.mkdir(exist_ok=True)
    (inputs / "note.md").write_text("RUNTIME_LOCAL", encoding="utf-8")
    work = tmp_path / "work"
    store = Session(work)
    output = tmp_path / "output"
    store.record.apply(grants=[str(inputs)], output=str(output))
    store.commit()
    runtime = ToolRuntime(
        store,
        Access([inputs], output, work),
        providers=kwargs.pop("providers", []),
        permission_mode=kwargs.pop("permission_mode", "auto"),
        **kwargs,
    )
    return inputs, output, store, runtime


def names(definitions):
    return [item["function"]["name"] for item in definitions]


def test_file_agent_gets_full_tool_set_and_reads(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    model = FakeModel(
        response(call("read_files", {"path": "note.md"}, "read")),
        Response(content="done"),
    )

    result = Runner(model, runtime).start("读取 note.md")

    assert result["status"] == "completed"
    offered = names(model.tool_definitions[0])
    assert {"read_files", "editor", "fetch_web_content"}.issubset(offered)
    assert store.record.calls['read']['state'] == 'succeeded'
    assert any((value['kind'] == 'file' for value in store.record.sources.values()))


def test_web_agent_searches_fetches_and_registers_source(tmp_path):
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
        response(call("fetch_web_content", {"query": "public"}, "query")),
        response(call("fetch_web_content", {"url": "https://public.test/"}, "fetch")),
        Response(content="web done"),
    )

    result = Runner(model, runtime).start("搜索并读取公开资料")

    assert result["status"] == "completed"
    assert {"fetch_web_content"}.issubset(names(model.tool_definitions[0]))
    assert len(model.contexts) == 3
    assert any((value['kind'] == 'web' for value in store.record.sources.values()))


def test_followup_reuses_previous_answer_and_web_tools(tmp_path):
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
        response(call("fetch_web_content", {"query": "Apple Watch"}, "follow_query")),
        response(call("fetch_web_content", {"url": "https://public.test/watch"}, "follow_initial_fetch")),
        Response(content="Apple Watch 已发布"),
    )
    first = Runner(first_model, runtime)
    assert first.start("苹果发布会有什么产品")["status"] == "completed"

    model = FakeModel(
        response(call("fetch_web_content", {"url": "https://public.test/watch"}, "follow_fetch")),
        Response(content="它的优势是续航"),
    )
    result = Runner(model, runtime).start("这个产品的优势是什么")

    assert result["status"] == "completed"
    assert result["answer"] == "它的优势是续航"
    assert {"fetch_web_content"}.issubset(names(model.tool_definitions[0]))
    assert [
        message.content
        for message in model.contexts[0]
        if message.role in {"user", "assistant"} and message.content is not None
    ][-3:] == [
        "苹果发布会有什么产品",
        "Apple Watch 已发布",
        "这个产品的优势是什么",
    ]
    assert [message.tool_call_id for message in model.contexts[0] if message.role == "tool"][-2:] == [
        "follow_query",
        "follow_initial_fetch",
    ]


def test_followup_reads_next_section_with_file_tool(tmp_path):
    inputs, _, store, runtime = make_runtime(tmp_path)
    (inputs / "note.md").write_text("第一部分\n第二部分：详细资料", encoding="utf-8")

    first = FakeModel(
        response(call("read_files", {"path": "note.md", "offset": 0, "limit": 4}, "first_read")),
        Response(content="第一部分概括"),
    )
    assert Runner(first, runtime).start("读取并概括第一部分")["status"] == "completed"

    followup = FakeModel(
        response(call("read_files", {"path": "note.md", "offset": 4, "limit": 100}, "second_read")),
        Response(content="第二部分的详细解释"),
    )
    result = Runner(followup, runtime).start("把第二部分再详细解释")

    assert result["status"] == "completed"
    assert "read_files" in names(followup.tool_definitions[0])
    assert store.record.calls['second_read']['state'] == 'succeeded'
    assert '第二部分' in store.record.calls['second_read']['result']['data']['files'][0]['text']
    assert [message.tool_call_id for message in followup.contexts[0] if message.role == "tool"][-1] == "first_read"


def test_three_followups_keep_session_context_and_reset_run_budget(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    prompts = ["第一问", "继续", "再说明", "最后一个问题"]
    answers = ["第一答", "第二答", "第三答", "第四答"]

    for prompt, answer in zip(prompts, answers):
        model = FakeModel(Response(content=answer, usage={"total_tokens": 1}))
        result = Runner(model, runtime, max_rounds=2, max_tools=3).start(prompt)
        assert result["status"] == "completed"
        assert store.record.rounds == 1
        assert store.record.tool_count == 0

    assert [message['content'] for message in store.record.messages if message['role'] != 'tool'] == [value for pair in zip(prompts, answers) for value in pair]
    assert len(store.record.history) == 3


def test_followup_compacts_long_previous_turn_and_continues(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    store.record.messages = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "x" * 81000, "tool_calls": None},
    ]
    store.record.status = "completed"
    store.commit()

    model = FakeModel(Response(content='继续完成'))
    model.context_config = {'max_input_tokens': 20000, 'strategy': 'basic'}
    result = Runner(model, runtime).start("基于上一轮继续")
    assert result['status'] == 'completed'
    assert store.record.context_checkpoint
    assert len(store.record.messages[1]['content']) == 81000


def test_network_confirmation_uses_network_message(tmp_path):
    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return []

    _, _, _, runtime = make_runtime(tmp_path, providers=[Provider()], permission_mode="user")
    model = FakeModel(
        response(call("fetch_web_content", {"query": "Apple"}, "network_call")),
    )

    result = Runner(model, runtime).start("查询 Apple")

    assert result["status"] == "awaiting_confirmation"
    assert result["answer"] == "需要确认公开网络请求"
    assert runtime.confirmation_message("editor") == "需要确认输出文件写入"
    assert runtime.confirmation_message("editor") == "需要确认输出文件写入"


def test_session_restore_keeps_messages_for_followup(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    assert Runner(FakeModel(Response(content="原回答")), runtime).start("原问题")["status"] == "completed"

    loaded = Session(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[])
    model = FakeModel(response(call("read_files", {"path": "note.md"}, "restored_read")), Response(content="追问回答"))
    result = Runner(model, restored).start("继续解释它")

    assert result["status"] == "completed"
    assert [message.content for message in model.contexts[0] if message.role in {"user", "assistant"}][-3:] == [
        "原问题",
        "原回答",
        "继续解释它",
    ]


@pytest.mark.parametrize("mode", ["user", "auto"])
def test_permission_matrix_and_session_restore(tmp_path, mode):
    _, _, store, runtime = make_runtime(tmp_path, permission_mode=mode)
    for name in ("editor", "fetch_web_content", "run_commands"):
        assert runtime.policy.requires_confirmation(runtime.registry.get(name), {}) == (mode == "user")
    assert not runtime.policy.requires_confirmation(runtime.registry.get("read_files"), {})
    restored = ToolRuntime(Session(store.root, store.id), runtime.access)
    assert restored.permission_mode == mode
    with pytest.raises(ValueError, match="权限模式"):
        restored.set_permission("sometimes")


def test_local_project_inspection_needs_no_approval(tmp_path):
    _, _, _, runtime = make_runtime(tmp_path, permission_mode="user")
    inspected = runtime.execute(call("run_commands", {"operation": "inspect", "cwd": "."}, "inspect"))
    assert inspected["ok"]
    command = {"operation": "run", "command": ["git", "status"]}
    assert runtime.execute(call("run_commands", command, "shell"))["pending"]


def test_host_file_access_follows_mode_and_survives_session_restore(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path, permission_mode="user")
    outside = tmp_path / "host.md"
    outside.write_text("host", encoding="utf-8")
    read_host = {"path": str(outside)}
    assert runtime.execute(call("read_files", read_host, "blocked"))["error"]["code"] == "permission_denied"
    runtime.set_permission("auto")
    assert runtime.execute(call("read_files", read_host, "read"))["ok"]
    target = tmp_path / "created.md"
    create = {"operation": "create", "path": str(target), "content": "outside"}
    assert runtime.execute(call("editor", create, "write"))["ok"]
    store.verify_sources()
    store.verify_artifacts()
    assert target.read_text() == "outside"
    runtime.set_permission("user")
    store.refresh_local_files()
    store.verify_artifacts()
    assert runtime.execute(call("editor", {"operation": "delete", "path": str(target)}, "blocked_delete"))["error"]["code"] == "permission_denied"
    assert target.exists()


def test_permission_switch_does_not_unfreeze_prepared_call(tmp_path):
    inputs, output, store, runtime = make_runtime(tmp_path, permission_mode="user")
    model = FakeModel(
        response(call("editor", {"operation": "copy", "source": "note.md", "path": "copy.md"}, "frozen_copy")),
        Response(content="handled"),
    )
    runner = Runner(model, runtime)
    assert runner.start("复制文件")["status"] == "awaiting_confirmation"

    runtime.set_permission("auto")
    (inputs / "note.md").write_text("CHANGED", encoding="utf-8")
    result = runner.confirm(True)

    assert result["status"] == "completed"
    assert not (output / "copy.md").exists()
    assert store.record.calls['frozen_copy']['result']['error']['code'] == 'confirmation_invalid'


def test_unknown_tool_is_unavailable_and_not_offered(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    result = runtime.execute(call("removed_tool", {}, "removed"))

    assert "removed_tool" not in names(runtime.definitions())
    assert result["error"]["code"] == "unknown_tool"


def test_confirmation_restart_restores_write(tmp_path):
    _, output, store, runtime = make_runtime(tmp_path, permission_mode="user")
    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "result.md", "content": "ok", "source_refs": []}, "write")),
    )
    runner = Runner(model, runtime)
    assert runner.start("创建文件")["status"] == "awaiting_confirmation"

    loaded = Session(store.root, store.id)
    restored = ToolRuntime(loaded, runtime.access, providers=[], permission_mode="user")

    result = Runner(FakeModel(Response(content="done")), restored).confirm(True)
    assert result["status"] == "completed"
    assert (output / "result.md").read_text(encoding="utf-8") == "ok"


def test_disabled_tool_cannot_execute_after_confirmation(tmp_path, monkeypatch):
    _, output, store, runtime = make_runtime(tmp_path, permission_mode="user")
    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "blocked.md", "content": "x", "source_refs": []}, "write_disabled")),
        Response(content="handled"),
    )
    runner = Runner(model, runtime)
    assert runner.start("创建文件")["status"] == "awaiting_confirmation"

    def disable_on_refresh():
        runtime.registry._items["editor"] = dataclasses.replace(
            runtime.registry.get("editor"), enabled=False
        )

    monkeypatch.setattr(runtime.registry, "refresh", disable_on_refresh)

    result = runner.confirm(True)

    assert result["status"] == "completed"
    assert not (output / "blocked.md").exists()
    assert store.record.calls['write_disabled']['result']['error']['code'] == 'tool_unavailable'


def test_final_ledger_failure_marks_completed_write_unknown(tmp_path, monkeypatch):
    _, output, store, runtime = make_runtime(tmp_path)

    def fail_finish(*args, **kwargs):
        raise OSError("fixture ledger failure")

    monkeypatch.setattr(runtime.ledger, "finish", fail_finish)
    result = runtime.execute(
        call("editor", {"operation": "create", "path": "unknown.md", "content": "written", "source_refs": []}, "unknown_write")
    )

    assert result["error"]["code"] == "unknown_execution"
    assert (output / "unknown.md").exists()
    assert store.record.calls['unknown_write']['state'] == 'unknown'
    with pytest.raises(ValueError, match="不确定"):
        Runner(FakeModel(Response(content="unsafe")), runtime).start("新任务")


def test_cancel_acknowledges_unknown_without_replaying_it(tmp_path):
    _, _, store, runtime = make_runtime(tmp_path)
    uncertain = call("editor", {"operation": "create", "path": "unknown.md", "content": "x", "source_refs": []}, "unknown")
    store.record.calls['unknown'] = {
        "call": uncertain,
        "state": "unknown",
        "result": runtime.error(uncertain, "unknown_execution", "结果不确定"),
    }
    store.record.status = "interrupted"
    store.record.pending = [uncertain]
    store.record.messages.extend([dict(role='user', content='创建文件'), dict(role='assistant', content=None, tool_calls=[uncertain])])
    store.commit()
    runner = Runner(FakeModel(Response(content="继续完成")), runtime)

    with pytest.raises(ValueError, match="处理确认或 /cancel"):
        runner.start("直接继续")

    assert runner.cancel()["status"] == "cancelled"
    delivered = json.loads(store.record.messages[-1]['content'])
    assert delivered['error']['code'] == 'unknown_execution'
    assert '未执行' not in delivered['error']['message']
    assert store.record.last_run_outcome['tool_activity'][-1]['executed'] is None
    assert runner.start("确认后继续")["status"] == "completed"
    assert store.record.calls['unknown']['state'] == 'unknown'
    assert store.record.calls['unknown']['acknowledged'] is True


@pytest.mark.parametrize(
    "tool_runtime",
    [
        [],
        {"active_skills": "read_files"},
        {"active_skills": ["read_files", 1]},
        {"active_skills": [str(index) for index in range(3)]},
        {"active_skills": [], "implementation": "unsafe"},
    ],
)
def test_invalid_session_tool_runtime_is_rejected_without_rewrite(tmp_path, tool_runtime):
    store = Session(tmp_path / "work")
    data = store.record.snapshot()
    data["tool_runtime"] = tool_runtime
    with store.database.transaction() as db:
        db.execute('UPDATE sessions SET state=? WHERE id=?', (json.dumps(data, ensure_ascii=False),store.id))
    before = store.database.path.read_bytes()

    with pytest.raises(ValueError, match="tool_runtime|active_skills"):
        Session(store.root, store.id)

    assert store.database.path.read_bytes() == before
