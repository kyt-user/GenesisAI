import json
import sys

from genesisai.agent.loop import Runner
from genesisai.shared.messages import Response, ToolCall
from test_acceptance import FakeModel
from test_development_continuation import development_env
from genesisai.app.evals.plan_steps import complete_plan


def tool_response(name, arguments, call_id):
    return Response(
        tool_calls=[ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))],
        finish_reason="tool_calls",
    )


def test_development_answer_without_file_change_is_bounded_partial(tmp_path):
    workspace, _, _, store, runtime = development_env(tmp_path)
    source = "<!doctype html><html><body>game</body></html>"

    result = Runner(FakeModel(*[Response(content=source) for _ in range(4)]), runtime).start("开始")

    assert result["status"] == "partial"
    assert "任务尚未完成" in result["answer"]
    assert store.record.run_runtime['recovery_counts']['acceptance'] == 4
    assert not (workspace / "index.html").exists()


def test_plan_only_cannot_complete_execution_request(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    model = FakeModel(
        tool_response(
            "plan",
            {'operation': 'update', 'expected_revision': 0, 'upsert': [
                {'id': 'create', 'kind': 'step', 'text': '创建 main.py'},
                {'id': 'accept', 'kind': 'acceptance', 'text': '语法检查通过', 'requires_verification': True}]},
            "plan",
        ),
        *[Response(content="计划已经完成。") for _ in range(4)],
    )

    result = Runner(model, runtime).start("按你的默认来")

    assert result["status"] == "partial"
    assert store.record.run_runtime['project_changed'] is False
    assert store.record.run_runtime['stop_reason'] == 'recovery_stalled'


def test_source_create_and_real_verification_can_complete(tmp_path):
    workspace, manager, _, store, runtime = development_env(tmp_path, planned=False)
    manager.update_plan(expected_revision=0, upsert=[
        {'id': 'create', 'kind': 'step', 'text': '创建 main.py'},
        {'id': 'accept', 'kind': 'acceptance', 'text': '语法检查通过', 'requires_verification': True},
    ])
    model = FakeModel(
        tool_response(
            "editor",
            {"operation": "create", "path": "main.py", "content": "print('ok')\n", "source_refs": []},
            "create",
        ),
        tool_response("run_commands", {"command": [sys.executable, "-m", "py_compile", "main.py"], "verification": "static"}, "syntax_check"),
        complete_plan(runtime),
        Response(content="已完成并通过验证。"),
    )

    result = Runner(model, runtime).start("开始")

    assert result["status"] == "completed"
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert store.record.run_runtime['project_changed'] is True
    assert store.record.run_runtime['verification_succeeded'] is True
    assert manager.__class__(store).active_task()["status"] == "completed"


def test_html_source_uses_deterministic_static_verification(tmp_path):
    workspace, manager, _, store, runtime = development_env(tmp_path)
    html = (
        "<!doctype html><html><body><input id=\"guess\">"
        "<script>const answer = 42; function check() { return answer; }</script>"
        "</body></html>\n"
    )
    model = FakeModel(
        tool_response(
            "editor",
            {"operation": "create", "path": "index.html", "content": html, "source_refs": []},
            "create_html",
        ),
        complete_plan(runtime),
        Response(content="页面已写入并通过静态验收。"),
    )

    result = Runner(model, runtime).start("开始")

    assert result["status"] == "completed"
    assert (workspace / "index.html").read_text(encoding="utf-8") == html
    create_result = store.record.calls['create_html']['result']['data']
    assert create_result["validation"]["framework"] == "frontend_static"
    assert create_result["validation"]["passed"] is True
    assert all((item['tool'] != 'run_commands' for item in store.record.run_runtime['tool_activity']))
    assert manager.__class__(store).active_task()["status"] == "completed"
