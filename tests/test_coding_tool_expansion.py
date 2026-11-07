import json
import sys
import time

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.extensions.lsp.extension import register_lsp
from genesisai.core.extensions.mcp.bridge import McpBridge
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from test_acceptance import FakeModel, call, response
import test_acceptance

env = test_acceptance.env


def test_search_raw_source_and_page_boundaries(env):
    root, _, _, runtime = env
    path = root / "line.json"
    path.write_text('{\n "needle": "needle",\n "other": "needle"\n}', encoding="utf-8")
    args = {"path": str(path), "query": "needle", "limit": 1}
    page = runtime.execute(call("search_codebase", args))["data"]
    hit = page["matches"][0]
    assert (hit["line"], hit["column"], hit["before"]) == (2, 3, ["{"])
    assert hit["after"] == [' "other": "needle"']
    page = runtime.execute(
        call("search_codebase", {**args, "offset": page["next_offset"]}, "page2")
    )["data"]
    assert page["matches"][0]["line"] == 3 and page["next_offset"] is None


def test_replace_preview_and_stale_approval(env):
    root, _, store, runtime = env
    path = root / "a.py"
    path.write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
    args = {
        "operation": "replace",
        "path": "a.py",
        "old_text": "one\ntwo",
        "new_text": "three\nfour",
    }
    runtime.set_permission("user")
    request = call("editor", args)
    assert "+three" in runtime.execute(request)["preview"]["diff"]
    approved = store.record.calls["c1"]["digest"]
    result = runtime.execute(request, approved)
    assert result["ok"] and path.read_bytes() == b"\xef\xbb\xbfthree\r\nfour\r\n"
    path.write_text("xx", encoding="utf-8")
    assert (
        runtime.execute(call("editor", {**args, "old_text": "x"}, "ambiguous"))[
            "error"
        ]["code"]
        == "patch_conflict"
    )
    request = call("editor", {**args, "old_text": "xx"}, "stale")
    runtime.execute(request)
    approved = store.record.calls["stale"]["digest"]
    path.write_bytes(b"xxy")
    assert runtime.execute(request, approved)["error"]["code"] == "confirmation_invalid"
    assert path.read_bytes() == b"xxy"


def test_batch_preflight_then_apply(env):
    root, _, store, runtime = env
    for name in ("a.py", "b.py"):
        (root / name).write_bytes(b"one\n")
    entries = [
        {"path": name, "patch": "@@ -1 +1 @@\n-one\n+two"} for name in ("a.py", "b.py")
    ]
    request = {"operation": "patch", "patches": entries}
    (root / "b.py").write_bytes(b"oops\n")
    assert not runtime.execute(call("editor", request))["ok"]
    assert (root / "a.py").read_bytes() == b"one\n" and not store.record.changes
    (root / "b.py").write_bytes(b"one\n")
    result = runtime.execute(call("editor", request, "success"))
    assert result["ok"], result
    assert len(result["data"]["applied"]) == len(result["observations"]) == 2


def test_background_job_survives_worker_and_requires_stop_approval(env):
    _, _, store, runtime = env
    runtime.set_permission("user")
    args = {
        "command": [
            sys.executable,
            "-u",
            "-c",
            "import time; print('ready'); time.sleep(60)",
        ],
        "background": True,
    }
    request = call("run_commands", args)
    try:
        assert runtime.execute(request)["pending"]
        assert runtime.jobs.status()["jobs"] == []
        result = runtime.execute(request, store.record.calls["c1"]["digest"])
        assert result["ok"] and result["observations"] == [], result
        job = result["data"]["job_id"]
        read = runtime.execute(
            call("read_job", {"job_id": job, "output": True, "limit": 3, "wait_seconds": .5}, "read")
        )
        assert read["data"]["text"] == "rea" and read["data"]["status"] == "running"
        stop = call("stop_job", {"job_id": job}, "stop")
        assert runtime.execute(stop)["pending"]
        result = runtime.execute(stop, store.record.calls["stop"]["digest"])
        assert result["data"]["status"] == "stopped"
        assert runtime.jobs.output(job, 3)["text"].startswith("dy")
    finally:
        runtime.close()


def test_command_exit_zero_is_not_test_evidence(env):
    _, _, _, runtime = env
    args = {"command": [sys.executable, "-c", "print('test passed')"]}
    result = runtime.execute(call("run_commands", args))
    assert (
        result["ok"] and not result["observations"] and "summary" not in result["data"]
    )


def test_ask_user_restores_and_consumes_the_original_call_once(env):
    inputs, output, store, runtime = env
    req = call("ask_user", {"question": "Choose a target", "options": ["cli", "web"]})
    runner = Runner(FakeModel(response(req)), runtime)
    first = runner.start("hi")
    assert first["status"] == "awaiting_input", first
    run_id = store.record.run_id
    restored = Session(store.root, store.id)
    second_runtime = ToolRuntime(
        restored, Access([inputs], output, store.root), providers=[]
    )
    second = Runner(FakeModel(Response(content="done")), second_runtime)
    assert second.resume()["status"] == "awaiting_input"
    assert second.answer("2")["status"] == "completed"
    assert restored.record.run_id == run_id and restored.record.tool_count == 1
    results = [m for m in restored.record.messages if m.get("tool_call_id") == "c1"]
    assert (
        len(results) == 1
        and json.loads(results[0]["content"])["data"]["answer"] == "web"
    )
    assert "pending_question" not in restored.record.run_runtime


@pytest.mark.parametrize(
    "operation,expected", [("definition", {1}), ("references", {1, 4})]
)
def test_real_python_lsp(env, tmp_path, operation, expected):
    pytest.importorskip("pylsp")
    root, _, _, runtime = env
    (root / "nav.py").write_text(
        "def hello():\n    pass\n\nhello()\n", encoding="utf-8"
    )
    cfg = {".py": {"command": [sys.executable, "-m", "pylsp"], "language": "python"}}
    (tmp_path / "lsp.json").write_text(json.dumps({"servers": cfg}), encoding="utf-8")
    runtime.lsp = register_lsp(tmp_path, runtime.registry, runtime.loader, state_root=runtime.store.root)
    args = {"operation": operation, "path": "nav.py", "line": 4, "column": 2}
    runtime.set_permission("user")
    req = call("lsp_query", args)
    assert runtime.execute(req)["preview"]["command"] == [sys.executable, "-m", "pylsp"]
    result = runtime.execute(req, runtime.store.record.calls[req["id"]]["digest"])
    assert result["ok"], result
    deadline, attempt = time.monotonic() + 15, 0
    while result['data']['status'] in {'queued', 'running'}:
        assert time.monotonic() < deadline, result
        time.sleep(.05)
        attempt += 1
        result = runtime.execute(call('read_lsp_result', {'query_id': result['data']['query_id']}, f'page{attempt}'))
        assert result['ok'], result
    assert result['data']['status'] == 'completed', result
    assert {item["line"] for item in result["data"]["locations"]} == expected
    runtime.lsp.close()
    runtime.close()


def test_mcp_resources_only_pagination_and_read(env, tmp_path):
    _, _, _, runtime = env

    class Resources:
        def request(self, method, params):
            if method == "server/discover":
                return {"supportedVersions": ["2026-07-28"], "capabilities": {"resources": {}}}
            if method == "resources/list":
                return (
                    {"resources": [{"uri": "mem:a"}], "nextCursor": "b"}
                    if not params
                    else {"resources": []}
                )
            if method == "resources/templates/list":
                return {"resourceTemplates": [{"uriTemplate": "mem:{id}"}]}
            assert method == "resources/read" and params == {"uri": "mem:a"}
            return {"contents": [{"uri": "mem:a", "text": "value"}]}

        def notify(self, method, params):
            pass

        def close(self):
            pass

    from genesisai.core.extensions.mcp import McpServerSpec
    bridge = McpBridge(
        tmp_path,
        runtime.registry,
        runtime.loader,
        transport_factory=lambda _: Resources(),
    ).configure({}, servers=[McpServerSpec("mem", "stdio", "fixture")])
    result = runtime.execute(call("connect_mcp", {"server": "mem"}))
    assert result["data"]["tools"] == [] and result["data"]["resources"]

    def run(name, ident, **params):
        result = runtime.execute(call(name, {"server": "mem", **params}, ident))
        assert result["ok"], result
        return result["data"]

    assert run("list_mcp_resources", "first")["nextCursor"] == "b"
    assert run("list_mcp_resources", "next", cursor="b")["resources"] == []
    assert run("list_mcp_resource_templates", "templates")["resourceTemplates"]
    assert run("read_mcp_resource", "read", uri="mem:a")["text"] == "value"
    bridge.close()


def test_mcp_validates_nested_constraints_and_propagates_tool_error(env, tmp_path):
    from test_extensions_seams import FakeTransport, fixture_servers

    _, _, store, runtime = env

    class Failing(FakeTransport):
        def request(self, method, params):
            if method == "tools/list":
                return {
                    "tools": [
                        {
                            "name": "update",
                            "inputSchema": {
                                "type": "object",
                                "$defs": {"value": {"enum": ["allowed"]}},
                                "required": ["items"],
                                "properties": {
                                    "items": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"$ref": "#/$defs/value"},
                                    }
                                },
                            },
                        }
                    ]
                }
            if method == "tools/call":
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "declined"}],
                }
            return super().request(method, params)

    bridge = McpBridge(
        tmp_path, runtime.registry, runtime.loader, transport_factory=Failing
    ).configure({}, servers=fixture_servers())
    bridge.connect("fixture")
    bad = runtime.execute(call("mcp_fixture_update", {"items": ["invalid"]}))
    assert bad["error"]["code"] == "validation_error"
    failed = runtime.execute(
        call("mcp_fixture_update", {"items": ["allowed"]}, "valid")
    )
    assert failed["error"]["code"] == "mcp_tool_error"
    assert store.record.calls["valid"]["state"] == "failed"


def test_cli_reopens_question_and_accepts_free_text(tmp_path, monkeypatch):
    from genesisai.app import cli
    from session_helpers import persisted_sessions

    prompt = response(call("ask_user", {"question": "哪个分支？"}))
    model = FakeModel(prompt, Response(content="已收到"))
    monkeypatch.setattr(cli, "build_model", lambda _: (model, False))
    commands = iter(["处理请求", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *a, **kw: next(commands))
    work = tmp_path / "work"
    assert cli.main(["--workspace", str(work)]) == 0
    (first,) = persisted_sessions()
    assert first["status"] == "awaiting_input"
    commands = iter(["/answer feature/navigation", "/exit"])
    assert cli.main(["--workspace", str(work), "--session", first["id"]]) == 0
    (last,) = persisted_sessions()
    assert last["status"] == "completed" and last["run_id"] == first["run_id"]
    assert last["calls"]["c1"]["result"]["data"]["answer"] == "feature/navigation"


def test_batch_reports_concurrent_change_after_first_write(env):
    from genesisai.core.extensions.tools.kernel.editor.implementation import (
        Implementation,
    )
    from genesisai.core.tools.base import PartialToolResult

    root, _, _, runtime = env
    paths = [root / f"module{i}.py" for i in range(3)]
    for path in paths:
        path.write_bytes(b"before\n")
    args = {
        "operation": "patch",
        "patches": [
            {"path": str(path), "patch": "@@ -1 +1 @@\n-before\n+after"}
            for path in paths
        ],
    }
    impl = Implementation()
    impl.prepare(runtime.context, args)
    paths[1].write_bytes(b"user change\n")
    result = impl.execute(runtime.context, args)
    assert (
        isinstance(result, PartialToolResult)
        and result.error["code"] == "batch_incomplete"
    )
    assert [change["path"] for change in result["applied"]] == [str(paths[0])]
    assert result["failed_path"] == str(paths[1]) and result["unattempted"] == [
        str(paths[2])
    ]
    assert [p.read_bytes() for p in paths] == [
        b"after\n",
        b"user change\n",
        b"before\n",
    ]


def test_background_timeout_and_restore(env):
    from time import monotonic, sleep
    from genesisai.core.jobs.service import JobService

    root, _, store, runtime = env
    args = [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(30)"]
    job = runtime.jobs.start(args, root, 1)["job_id"]
    try:
        deadline = monotonic() + 5
        while (
            runtime.jobs.status(job)["status"] in {"queued", "running"} and monotonic() < deadline
        ):
            sleep(0.05)
        restored = JobService(store, str)
        assert restored.status(job)["status"] == "timed_out"
        assert "started" in restored.output(job)["text"]
    finally:
        runtime.close()


def test_cancel_question_closes_ledger_without_executing(env):
    _, _, store, runtime = env
    model = FakeModel(response(call("ask_user", {"question": "Which branch?"})))
    runner = Runner(model, runtime)
    assert runner.start("hi")["status"] == "awaiting_input"
    assert store.record.calls["c1"]["state"] == "awaiting_input"
    assert runner.cancel()["status"] == "cancelled"
    entry = store.record.calls["c1"]
    assert entry["state"] == "failed" and not entry["executed"]
    assert not store.record.pending and "pending_question" not in store.record.run_runtime
