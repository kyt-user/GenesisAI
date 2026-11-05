from session_helpers import persisted_sessions
"""CLI 展示层及预留命令的行为验证。"""

from genesisai.shared.paths import application_home


from io import StringIO
import json

import pytest
from rich.console import Console

from genesisai.app.cli import main
from genesisai.app.terminal_view import CliSnapshot, CliView
from genesisai.shared.messages import Response
from genesisai.core.persistence.session import Session
from test_acceptance import FakeModel, call, response


def recording_view():
    stream = StringIO()
    return CliView(Console(file=stream, width=120, force_terminal=False)), stream


def snapshot():
    return CliSnapshot(
        session_id="session_12345678",
        model_name="test-model",
        workspace="D:/project/workspace",
        input_roots=[],
        output="D:/project/output",
        permission_mode="user",
        registered_tools=10,
        enabled_tools=3,
    )


def test_cli_home_shows_workspace_and_task_entry():
    view, stream = recording_view()

    view.render_home(snapshot().workspace, [])

    output = stream.getvalue()
    assert "GenesisAI" in output
    assert 'D:/project/workspace' in output
    assert '输入 / 查看操作' in output
    assert '/auth 切换模型提供商' in output
    assert 'Tools:' not in output and 'Skill:' not in output


def test_command_control_sequences_are_inert_in_live_output_and_details():
    view, stream = recording_view()
    unsafe = '中文\tA\x1b[2J\x1b[3D\x1b]0;title\x07\x9b2J\x9dtitle\x9c\n'
    view.render_progress('command_output', {'text': unsafe})
    view.show_details = True
    view.render_progress('tool_result', {'ok': True, 'data': {'stdout': unsafe, 'cwd': unsafe}})
    shown = stream.getvalue()
    assert '中文' in shown and 'title' in shown
    assert not any(ch in shown for ch in '\x1b\x07\x9b\x9c\x9d')


def test_confirmation_card_keeps_target_and_actions_visible():
    view, stream = recording_view()

    view.render_confirmation(
        "editor",
        {"target": "D:/project/output/report.md", "operation": "create", "args": {"content": "# 报告"}},
    )

    output = stream.getvalue()
    assert "D:/project/output/report.md" in output
    assert "/approve" in output
    assert "/reject" in output
    assert "参数已冻结" in output


def test_tools_view_shows_all_runtime_layers():
    view, stream = recording_view()
    view.render_tools({
        "enabled": [{"name": "read_files", "category": "kernel", "description": "读取"}],
        "disabled": [{"name": "fetch_web_content", "category": "kernel", "description": "搜索", "reason": "已禁用"}],
    })

    output = stream.getvalue()
    assert all(label in output for label in ("Enabled", "Disabled"))
    assert "fetch_web_content（已禁用）" in output
    assert "read_files" in output


def test_local_inspection_commands_do_not_call_model(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    model = FakeModel()
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["/model", "/permissions", "/tools", "/skills", "/diff", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))

    assert main(["--workspace", str(tmp_path / "work")]) == 0
    assert model.contexts == []


def test_help_and_dispatch_remove_retired_commands(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    view, stream = recording_view()
    view.render_help()
    help_text = stream.getvalue()
    for retired in ('/undo', '/doctor', '/clean'):
        assert retired not in help_text
    for active in ('/sessions', '/compact', '/skills', '/workflow', '/logs', '/project'):
        assert active in help_text
    model = FakeModel()
    monkeypatch.setattr(cli, 'Console', lambda **kwargs: view.console)
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    commands = iter(['/undo', '/undo apply', '/doctor', '/clean', '/clean run', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *args, **kwargs: next(commands))
    assert main(['--workspace', str(tmp_path / 'work')]) == 0
    assert model.contexts == []
    assert stream.getvalue().count('未知命令；使用 /help') == 5


def test_new_command_creates_fresh_session_and_preserves_old_one(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    model = FakeModel(Response(content="old answer"), Response(content="new answer"))
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["old question", "/new", "new question", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0

    sessions = persisted_sessions()
    assert len(sessions) == 2
    payloads = sessions
    old = next(item for item in payloads if any(message.get("content") == "old question" for message in item["messages"]))
    new = next(item for item in payloads if any(message.get("content") == "new question" for message in item["messages"]))
    assert any(message.get("content") == "old answer" for message in old["messages"])
    assert all(message.get("content") not in {"old question", "old answer"} for message in new["messages"])
    assert new["tool_runtime"] == {"active_skills": []}
    assert old["id"] != new["id"]
    assert old["output"] == new["output"] == str(work.resolve())

    restored = Session(application_home(), old["id"], workspace=work)
    assert any((message.get('content') == 'old answer' for message in restored.record.messages))
    assert restored.record.id == old['id']


def test_permissions_command_switches_session_mode(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    view, stream = recording_view()
    model = FakeModel()
    monkeypatch.setattr(cli, "Console", lambda **kwargs: view.console)
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter([
        "/permissions auto",
        "/status",
        "/permissions",
        "/permissions user",
        "/permissions",
        "/exit",
    ])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))

    assert main(["--workspace", str(tmp_path / "work")]) == 0

    output = stream.getvalue()
    assert "公开网络" in output
    assert "自动执行" in output
    assert "逐次确认" in output
    assert "工作区外" in output
    assert model.contexts == []


@pytest.mark.parametrize("approval", ["approve", "yes", "y"])
def test_plain_approve_alias_continues_pending_call(tmp_path, monkeypatch, approval):
    import genesisai.app.cli as cli

    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "alias.md", "content": "ok", "source_refs": []}, "write_alias")),
        Response(content="done"),
    )
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["create", approval, "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0
    assert (work / "alias.md").read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize("rejection", ["reject", "no", "n"])
def test_plain_reject_alias_rejects_only_pending_call(tmp_path, monkeypatch, rejection):
    import genesisai.app.cli as cli

    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "reject.md", "content": "no", "source_refs": []}, "write_reject")),
        Response(content="rejected"),
    )
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["create", rejection, "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0
    assert not (work / "reject.md").exists()
    session = persisted_sessions()[0]
    assert session["calls"]["write_reject"]["state"] == "failed"
    assert session["calls"]["write_reject"]["result"]["error"]["code"] == "confirmation_rejected"


def test_new_command_is_blocked_while_confirmation_is_pending(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    view, stream = recording_view()
    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "pending.md", "content": "x", "source_refs": []}, "pending_new")),
    )
    monkeypatch.setattr(cli, "Console", lambda **kwargs: view.console)
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["create", "/new", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0

    assert len(persisted_sessions()) == 1
    assert "处理确认" in stream.getvalue()


def test_new_session_keeps_grants_resets_runtime_permission_and_refreshes_ui(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    view, stream = recording_view()
    model = FakeModel(Response(content="old"))
    monkeypatch.setattr(cli, "Console", lambda **kwargs: view.console)
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["old", "/permissions auto", "/new", "/permissions", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"
    inputs = work / "inputs"
    inputs.mkdir(parents=True)

    assert main(["--workspace", str(work), "--input", str(inputs)]) == 0

    payloads = persisted_sessions()
    assert len(payloads) == 1
    assert all(item["grants"] == [str(inputs.resolve())] for item in payloads)
    assert stream.getvalue().splitlines().count("GenesisAI") == 2
    assert "逐次确认" in stream.getvalue()


def test_new_session_resets_session_and_dispatches_full_tool_set(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    model = FakeModel(
        Response(content="old done"),
        Response(content="new done"),
    )
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["old task", "/new", "new task", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0

    tool_names = lambda definitions: [item["function"]["name"] for item in definitions]
    assert "read_files" in tool_names(model.tool_definitions[0])
    assert tool_names(model.tool_definitions[0]) == tool_names(model.tool_definitions[1])
    payloads = persisted_sessions()
    new = next(item for item in payloads if any(message.get("content") == "new task" for message in item["messages"]))
    assert new["tool_runtime"] == {"active_skills": []}


def test_other_plain_text_cannot_approve_pending_call(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    view, stream = recording_view()
    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "blocked.md", "content": "x", "source_refs": []}, "not_approval")),
        Response(content="done"),
    )
    monkeypatch.setattr(cli, "Console", lambda **kwargs: view.console)
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["create", "maybe", "/reject", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work)]) == 0

    assert not (work / "blocked.md").exists()
    assert "处理确认" in stream.getvalue()


def test_auto_mode_allows_output_creation(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    model = FakeModel(
        response(call("editor", {"operation": "create", "path": "allowed.md", "content": "ok", "source_refs": []}, "yes_writes")),
        Response(content="done"),
    )
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["create", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work), "--permissions", "auto"]) == 0

    assert (work / "allowed.md").read_text(encoding="utf-8") == "ok"
    session = persisted_sessions()[0]
    assert session["status"] == "completed"


def test_auto_mode_allows_public_search(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from genesisai.core.tools.tool_runtime import ToolRuntime as RealToolRuntime

    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return [{"title": "Apple", "url": "https://public.test/", "snippet": "result"}]

    def runtime_factory(store, access, **kwargs):
        return RealToolRuntime(store, access, providers=[Provider()], **kwargs)

    model = FakeModel(
        response(call("fetch_web_content", {"query": "Apple"}, "yes_search")),
        Response(content="done"),
    )
    monkeypatch.setattr(cli, "ToolRuntime", runtime_factory)
    monkeypatch.setattr(cli, "build_model", lambda path: (model, False))
    commands = iter(["search", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    work = tmp_path / "work"

    assert main(["--workspace", str(work), "--permissions", "auto"]) == 0

    session = persisted_sessions()[0]
    assert session["status"] == "completed"
    assert session["calls"]["yes_search"]["state"] == "succeeded"


def test_remote_model_and_public_search_permissions_are_separate(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from genesisai.core.tools.tool_runtime import ToolRuntime as RealToolRuntime

    class Provider:
        name = "fixture"

        def search(self, query, limit=5):
            return []

    def runtime_factory(store, access, **kwargs):
        return RealToolRuntime(store, access, providers=[Provider()], **kwargs)

    pending_model = FakeModel(
        response(call("fetch_web_content", {"query": "public"}, "remote_search")),
    )
    monkeypatch.setattr(cli, "ToolRuntime", runtime_factory)
    monkeypatch.setattr(cli, "build_model", lambda path: (pending_model, True))
    work = tmp_path / "allowed"
    assert main([
        "--workspace", str(work), "--prompt", "research"
    ]) == 2
    sessions = persisted_sessions()
    session = next(item for item in sessions if item['workspace'] == str(work.resolve()))
    assert session["status"] == "awaiting_confirmation"
    assert session["calls"]["remote_search"]["state"] == "prepared"

