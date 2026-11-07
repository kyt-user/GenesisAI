import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

from genesisai.core.capabilities.store import CapabilityStore
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.ids import uid
from genesisai.shared.security import Access, ToolError


def tool_package(name="double_value", scope="project"):
    return {
        "kind": "tool", "name": name, "description": "Double a number", "scope": scope,
        "definition": {"runner": "python", "entry": "tool.py", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False},
                       "output_schema": {"type": "object", "required": ["value", "cwd"]}, "routes": ["coding"]},
        "files": {"tool.py": "import json, os, sys\nfrom pathlib import Path\nargs=json.load(sys.stdin)\nPath('output.txt').write_text(str(args['value']*2))\njson.dump({'value': args['value']*2, 'cwd': os.getcwd()}, open(os.environ['GENESISAI_RESULT_PATH'], 'w', encoding='utf-8'))\n"},
        "tests": [{"name": "double", "input": {"value": 3}, "expect": {"type": "object", "properties": {"value": {"const": 6}}, "required": ["value"]}},
                  {"name": "invalid input", "input": {"value": "bad"}, "expect_error": "validation_error"}],
    }


@pytest.fixture
def capability_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = Session(tmp_path / "app", workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.commit()
    runtime = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), permission_mode="auto", providers=[])
    yield runtime
    runtime.jobs.stop_all()
    runtime.close()


@pytest.fixture
def background_trials(capability_runtime, monkeypatch):
    runtime = capability_runtime
    wait = runtime.jobs.wait
    def short_foreground(job_id, seconds=0, cancel_on_interrupt=False, emit_output=False):
        return wait(job_id, min(seconds, .01) if cancel_on_interrupt else seconds,
                    cancel_on_interrupt=cancel_on_interrupt, emit_output=emit_output)
    monkeypatch.setattr(runtime.jobs, "wait", short_foreground)
    return runtime


def invoke(runtime, name, args, *, approval=None, call=None, wait=False):
    call = call or {"id": uid("case"), "name": name, "arguments": json.dumps(args)}
    result = runtime.execute(call, approved=approval)
    if wait and result.get("ok") and result.get("data", {}).get("kind") in {"capability_test", "capability_try"}:
        identity = result["data"]["job_id"]
        deadline = time.monotonic() + 90
        while result.get("ok") and result["data"].get("status") in {"queued", "running", "stopping"}:
            assert time.monotonic() < deadline, result
            _, result = invoke(runtime, "read_job", {"job_id": identity, "wait_seconds": 20})
        if result.get("ok"):
            result = {**result, "data": result["data"]["result"]}
    return call, result


def referenced_package(package, workspace):
    """Write test inputs as ordinary authoring files; the API gets references."""
    value = deepcopy(package)
    root = Path(workspace) / uid("draft_inputs")
    root.mkdir(parents=True)
    for number, files in enumerate([value["files"], *(case.get("fixtures", {}) for case in value["tests"])]):
        for index, (name, content) in enumerate(files.items()):
            path = root / f"{number}_{index}.asset"
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
            files[name] = {"path": str(path)}
    return value


def draft_args(package, workspace):
    return {k: v for k, v in referenced_package(package, workspace).items() if k != "kind"}


def change_args(package, workspace):
    return {k: v for k, v in referenced_package(package, workspace).items() if k in {"description", "definition", "files", "tests"}}


def freeze(runtime, item):
    _, result = invoke(runtime, "freeze_capability", {"id": item["id"], "expected_revision": item["revision"]})
    assert result["ok"], result
    return result["data"]


def created(runtime, package):
    _, result = invoke(runtime, "create_" + package["kind"], draft_args(package, runtime.store.workspace))
    assert result["ok"], result
    return freeze(runtime, result["data"])


def reference(item):
    return {key: item[key] for key in ("id", "revision", "digest")}


def accept(runtime, item):
    call, result = invoke(runtime, "register_capability", reference(item))
    assert result["pending"], result
    assert runtime.permission_mode == "auto"
    approved = runtime.store.record.calls[call["id"]]["digest"]
    _, result = invoke(runtime, "register_capability", {}, approval=approved, call=call)
    assert result["ok"], result
    return result


def test_tool_draft_test_acceptance_execute_and_restore(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    assert "double_value" not in runtime.registry
    _, denied = invoke(runtime, "register_capability", reference(item))
    assert denied["error"]["code"] == "capability_untested"
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["ok"] and tested["data"]["report"]["passed"], tested
    assert not (runtime.store.workspace / "output.txt").exists()
    with pytest.raises(ToolError, match="验收"):
        runtime.capabilities.register(reference(item))
    accept(runtime, item)
    _, result = invoke(runtime, "double_value", {"value": 7})
    assert result["ok"] and result["data"]["value"]["value"] == 14, result
    assert Path(result["data"]["value"]["cwd"]) == runtime.store.workspace
    assert (runtime.store.workspace / "output.txt").read_text() == "14"
    assert any(t["name"] == "double_value" for t in runtime.inventory()["external"])
    resumed = ToolRuntime(Session(runtime.store.root, runtime.store.id), runtime.access, permission_mode="auto", providers=[])
    try:
        _, result = invoke(resumed, "double_value", {"value": 2})
        assert result["ok"] and result["data"]["value"]["value"] == 4
    finally:
        resumed.close()


def test_failed_test_cannot_register_and_revision_invalidates_approval(capability_runtime):
    runtime = capability_runtime
    package = tool_package()
    package["tests"][0]["expect"]["properties"]["value"]["const"] = 9
    item = created(runtime, package)
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["ok"] and not tested["data"]["report"]["passed"]
    _, denied = invoke(runtime, "register_capability", reference(item))
    assert denied["error"]["code"] == "capability_untested"
    _, changed = invoke(runtime, "revise_capability", {"id": item["id"], "expected_revision": 1, "changes": change_args(tool_package(), runtime.store.workspace)})
    item = freeze(runtime, changed["data"])
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"]
    call, pending = invoke(runtime, "register_capability", reference(item))
    assert pending["pending"]
    approval = runtime.store.record.calls[call["id"]]["digest"]
    _, modified = invoke(runtime, "revise_capability", {"id": item["id"], "expected_revision": 2, "changes": change_args(tool_package(), runtime.store.workspace)})
    assert modified["ok"]
    _, stale = invoke(runtime, "register_capability", {}, call=call, approval=approval)
    assert not stale["ok"] and "double_value" not in runtime.registry


def test_scope_revision_conflicts_and_bundle_tampering(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    other = CapabilityStore(runtime.store.root, "project_other")
    assert other.list() == []
    with pytest.raises(ToolError, match="作用域"):
        other.get(item["id"])
    user = created(runtime, tool_package("shared_tool", scope="user"))
    assert other.get(user["id"])["package"]["name"] == "shared_tool"
    with pytest.raises(ToolError, match="修改"):
        runtime.capabilities.store.revise(item["id"], 9, runtime.capabilities.store.get(item["id"])["package"])
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"]
    accept(runtime, item)
    bundle = runtime.capabilities.store.materialize(runtime.capabilities.store.get(item["id"]))
    (bundle / "tool.py").write_text("raise RuntimeError('tampered')", encoding="utf-8")
    _, result = invoke(runtime, "double_value", {"value": 1})
    assert not result["ok"] and "double_value" not in runtime.registry
    assert runtime.capability_diagnostics


@pytest.mark.parametrize("path", ["../escape.py", "C:/escape.py", "sub\\escape.py", "CON.txt", "x/../y.py"])
def test_package_paths_cannot_escape_or_alias(capability_runtime, path):
    package = tool_package()
    package["files"][path] = "no"
    _, result = invoke(capability_runtime, "create_tool", draft_args(package, capability_runtime.store.workspace))
    assert not result["ok"]
    assert not capability_runtime.capabilities.store.list()


def mcp_package():
    return {
        "kind": "mcp", "name": "echo_service", "description": "Local echo service", "scope": "project",
        "definition": {"transport": "stdio", "command": ["{python}", "-B", "{bundle}/server.py"], "read_only": True, "request_timeouts": {"first_response": 5, "idle": 5}},
        "files": {"server.py": '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'server/discover':
        result = {'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'echo', 'description': 'Echo', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}}]}
    else:
        result = {'content': [{'type': 'text', 'text': request['params']['arguments']['text']}]}
    result['resultType'] = 'complete'
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''},
        "tests": [{"name": "real echo", "input": {"method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello"}}},
                   "expect": {"type": "object", "properties": {"text": {"const": "hello"}}, "required": ["text"]}}],
    }


def test_mcp_draft_real_discovery_call_registration_and_owner(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, mcp_package())
    assert "connect_mcp" not in runtime.registry
    _, result = invoke(runtime, "test_capability", reference(item))
    assert result["ok"] and result["data"]["report"]["passed"], result
    assert "mcp_echo_service_echo" not in runtime.registry
    accept(runtime, item)
    _, connected = invoke(runtime, "connect_mcp", {"server": "echo_service"})
    assert connected["ok"], connected
    _, echoed = invoke(runtime, "mcp_echo_service_echo", {"text": "registered"})
    assert echoed["ok"] and echoed["data"]["text"] == "registered", echoed
    descriptor = runtime.registry.get("mcp_echo_service_echo")
    assert descriptor.source == "external" and descriptor.provider == "echo_service"
    restored = ToolRuntime(Session(runtime.store.root, runtime.store.id), runtime.access, permission_mode="auto", providers=[])
    try:
        _, echoed = invoke(restored, "mcp_echo_service_echo", {"text": "restored"})
        assert echoed["ok"] and echoed["data"]["text"] == "restored"
    finally:
        restored.close()
    runtime.capabilities.store.disable(item["id"])
    runtime.refresh_capabilities()
    assert "mcp_echo_service_echo" not in runtime.registry and not runtime.mcp.transports


def skill_package():
    return {"kind": "skill", "name": "make_note", "description": "Write a fixed note", "scope": "project",
            "definition": {"entry": "SKILL.md", "stages": {"run": {"depends_on": [], "required_tools": ["editor"], "optional_tools": []}}},
            "files": {"SKILL.md": "使用 editor 创建 note.txt，内容严格为 hello。完成后报告结果。"},
            "tests": [{"name": "write a note", "input": {"stage": "run", "prompt": "按已加载的 Skill 生成 note.txt 文档"},
                       "expect": {"type": "object", "properties": {"artifacts": {"properties": {"note.txt": {"properties": {"sha256": {"const": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}, "size": {"const": 5}, "kind": {"const": "text"}}, "required": ["sha256", "size", "kind"]}}, "required": ["note.txt"]}}, "required": ["artifacts"]}}]}


def test_skill_trials_execute_agent_and_check_real_artifacts(capability_runtime):
    from test_acceptance import FakeModel, call, response
    from genesisai.shared.messages import Response
    runtime = capability_runtime
    item = created(runtime, skill_package())
    _, untested = invoke(runtime, "test_capability", reference(item))
    assert not untested["data"]["report"]["passed"]
    assert untested["data"]["report"]["cases"][0]["code"] == "model_required"
    model = FakeModel(response(call("editor", {"operation": "create", "path": "note.txt", "content": "hello"}, "write")), Response(content="已生成"))
    runtime.bind_model(model)
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["ok"] and tested["data"]["report"]["passed"], tested
    assert not (runtime.store.workspace / "note.txt").exists()
    assert "make_note" not in runtime.skills.items
    trial_result = tested['data']['report']['cases'][0]['result']
    assert 'files' not in trial_result
    _, read_result = invoke(runtime, 'read_capability_artifact',
                            {'id': trial_result['artifacts']['note.txt']['id'], 'mode': 'text'})
    assert read_result['ok'] and read_result['data']['text'] == 'hello'
    trial_session = Session(trial_result['state_root'], trial_result['session_id'])
    called = {entry['call']['name'] for entry in trial_session.record.calls.values()}
    assert 'editor' in called and called <= {'editor', 'attempt_completion', 'read_job', 'stop_job'}
    assert not model.contexts and not model.tool_definitions
    accept(runtime, item)
    skill = runtime.skills.describe("make_note", include_content=True)
    assert skill["source"] == "external" and skill["scope"] == "project"
    _, result = invoke(runtime, "use_skill", {"name": "make_note", "stage": "run"})
    assert result["ok"] and "hello" in result["data"]["content"]
    restored = ToolRuntime(Session(runtime.store.root, runtime.store.id), runtime.access, permission_mode="auto", providers=[])
    try:
        _, reused = invoke(restored, "use_skill", {"name": "make_note", "stage": "run"})
        assert reused["ok"] and "hello" in reused["data"]["content"]
    finally:
        restored.close()


def test_skill_large_and_binary_outputs_pass_actual_isolated_trial(capability_runtime):
    import hashlib
    import sys
    from test_acceptance import FakeModel, call, response
    from genesisai.shared.messages import Response
    runtime = capability_runtime
    package = skill_package()
    package['definition']['stages']['run']['required_tools'] = ['run_commands']
    package['files']['SKILL.md'] = '使用 Python 生成 large.txt 和 data.bin，完成后报告。'
    body = b'x' * 600000 + b'TAIL'
    binary = bytes(range(256)) * 1000
    expected = {}
    for name, content in [('large.txt', body), ('data.bin', binary)]:
        expected[name] = {'properties': {'sha256': {'const': hashlib.sha256(content).hexdigest()},
                                        'size': {'const': len(content)}}, 'required': ['sha256', 'size']}
    package['tests'] = [{'name': 'large outputs', 'input': {'stage': 'run', 'prompt': '按照 Skill 生成大段文字和二进制资料'},
                         'expect': {'properties': {'artifacts': {'properties': expected, 'required': list(expected)}},
                                    'required': ['artifacts']}}]
    item = created(runtime, package)
    code = "from pathlib import Path; Path('large.txt').write_bytes(b'x'*600000+b'TAIL'); Path('data.bin').write_bytes(bytes(range(256))*1000)"
    runtime.bind_model(FakeModel(response(call('run_commands', {'command': [sys.executable, '-c', code]}, 'generate')),
                                 Response(content='已生成资料')))
    _, tested = invoke(runtime, 'test_capability', reference(item))
    assert tested['ok'] and tested['data']['report']['passed'], tested
    artifacts = tested['data']['report']['cases'][0]['result']['artifacts']
    _, page = invoke(runtime, 'read_capability_artifact', {'id': artifacts['large.txt']['id'],
                                                         'mode': 'text', 'offset': 600000, 'limit': 4})
    assert page['ok'] and page['data']['text'] == 'TAIL'
    assert not (runtime.store.workspace / 'large.txt').exists()


def test_revision_keeps_accepted_version_until_new_acceptance(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    invoke(runtime, "test_capability", reference(item))
    accept(runtime, item)
    package = tool_package()
    package["files"]["tool.py"] = package["files"]["tool.py"].replace("*2", "*3")
    package["tests"][0]["expect"]["properties"]["value"]["const"] = 9
    _, revised = invoke(runtime, "revise_capability", {"id": item["id"], "expected_revision": 1, "changes": change_args(package, runtime.store.workspace)})
    _, old = invoke(runtime, "double_value", {"value": 2})
    assert old["data"]["value"]["value"] == 4
    item = freeze(runtime, revised["data"])
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"]
    accept(runtime, item)
    _, new = invoke(runtime, "double_value", {"value": 2})
    assert new["data"]["value"]["value"] == 6


def test_retest_invalidates_pending_and_active_acceptance(background_trials):
    runtime = background_trials
    item = created(runtime, tool_package())
    invoke(runtime, "test_capability", reference(item), wait=True)
    call, pending = invoke(runtime, "register_capability", reference(item))
    assert pending["pending"]
    approval = runtime.store.record.calls[call["id"]]["digest"]
    invoke(runtime, "test_capability", reference(item), wait=True)
    _, stale = invoke(runtime, "register_capability", {}, call=call, approval=approval)
    assert stale["error"]["code"] == "confirmation_invalid"
    accept(runtime, item)
    invoke(runtime, "test_capability", reference(item), wait=True)
    assert runtime.capabilities.inspect(item["id"])["state"] == "tested"
    runtime.refresh_capabilities()
    assert "double_value" not in runtime.registry
    accept(runtime, item)
    assert "double_value" in runtime.registry


def test_manual_import_try_revise_export_and_rejected_acceptance(capability_runtime):
    from genesisai.app.capability_commands import execute_capability_command, creation_prompt
    runtime = capability_runtime
    def command(text, confirm=lambda preview: True):
        return execute_capability_command(runtime, text, confirm=confirm)
    package = tool_package()
    (runtime.store.workspace / "input package.json").write_text(json.dumps(referenced_package(package, runtime.store.workspace)), encoding="utf-8")
    item = command('/capabilities import "input package.json"')
    identity = item["id"]
    command(f"/capabilities freeze {identity}")
    assert "create_tool" in creation_prompt("/create_tool 将输入翻倍")
    assert command("/capabilities list")["drafts"][0]["state"] == "draft"
    (runtime.store.workspace / "trial.json").write_text(json.dumps({"input": {"value": 5}}))
    trial = command(f"/capabilities try {identity} trial.json")
    assert trial["ok"] and trial["data"]["result"]["value"]["value"] == 10
    assert not trial["data"]["registration_eligible"]
    assert not (runtime.store.workspace / "output.txt").exists()
    assert command(f"/capabilities test {identity}")["data"]["report"]["passed"]
    rejected = command(f"/capabilities register {identity}", confirm=lambda preview: False)
    assert rejected["error"]["code"] == "confirmation_rejected"
    assert "double_value" not in runtime.registry
    assert command(f"/capabilities register {identity}")["ok"]
    assert command("/capabilities list")["external"]["tools"]
    command(f"/capabilities export {identity} exported.json")
    exported = json.loads((runtime.store.workspace / "exported.json").read_text(encoding="utf-8"))
    assert exported["definition"] == package["definition"]
    assert (runtime.store.workspace / exported["files"]["tool.py"]["path"]).read_text(encoding="utf-8") == package["files"]["tool.py"]
    (runtime.store.workspace / "changes.json").write_text(json.dumps({"description": "Updated description"}), encoding="utf-8")
    updated = command(f'/capabilities revise {identity} 1 "changes.json"')
    assert updated["revision"] == 2 and updated["state"] == "draft"
    command(f"/capabilities disable {identity}")
    assert "double_value" not in runtime.registry


def test_agent_build_test_pause_restore_and_accept(background_trials):
    from test_acceptance import FakeModel, call, response
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response
    runtime = background_trials
    package = tool_package()
    def next_step(name):
        def reply(messages):
            item = runtime.capabilities.inspect()["drafts"][0]
            args = {"id": item["id"], "expected_revision": item["revision"]} if name == "freeze_capability" else reference(item)
            return response(call(name, args, name))
        return reply
    def collect_trial(messages):
        pending = runtime.store.record.run_runtime["pending_execution_jobs"]
        if pending:
            return response(call("read_job", {"job_id": pending[0], "wait_seconds": 30}, "collect_trial"))
        return response(call("inspect_capabilities", {}, "inspect_trial"))
    model = FakeModel(response(call("create_tool", draft_args(package, runtime.store.workspace), "create")),
                      next_step("freeze_capability"), next_step("test_capability"),
                      response(call("discover_tools", {"query": "read_job"}, "discover_jobs")),
                      collect_trial, next_step("register_capability"))
    result = Runner(model, runtime).start("创造模式：创建可复用的 Tool 将整数翻倍，完成实际测试后让我验收注册")
    assert result["status"] == "awaiting_confirmation", result
    assert "double_value" not in runtime.registry
    assert "create_tool" in {t["function"]["name"] for t in model.tool_definitions[0]}
    resumed = ToolRuntime(Session(runtime.store.root, runtime.store.id), runtime.access, permission_mode="auto", providers=[])
    try:
        result = Runner(FakeModel(Response(content="double_value 已通过实际测试并注册。")), resumed).confirm(True)
        assert result["status"] == "completed", result
        _, doubled = invoke(resumed, "double_value", {"value": 9})
        assert doubled["data"]["value"]["value"] == 18
    finally:
        resumed.close()


def test_real_timeout_report_and_remote_schema_never_fetches(background_trials):
    runtime = background_trials
    package = tool_package()
    package["definition"]["timeout_seconds"] = 1
    package["files"]["tool.py"] = "import time\ntime.sleep(30)\n"
    package["tests"] = package["tests"][:1]
    item = created(runtime, package)
    _, tested = invoke(runtime, "test_capability", reference(item), wait=True)
    assert not tested["data"]["report"]["passed"]
    assert tested["data"]["report"]["cases"][0]["code"] == "tool_timeout"
    from genesisai.core.capabilities.contracts import schema_validator
    with pytest.raises(Exception, match="Unresolvable"):
        schema_validator({"$ref": "https://127.0.0.1:1/never-fetch"}).validate({})


def test_mcp_environment_refresh_without_storing_credentials(capability_runtime, monkeypatch):
    runtime = capability_runtime
    monkeypatch.setenv("GENESIS_CAPABILITY_TEST_SECRET", "first-secret")
    package = mcp_package()
    package["definition"]["env_from"] = {"SERVICE_SECRET": "GENESIS_CAPABILITY_TEST_SECRET"}
    item = created(runtime, package)
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"]
    accept(runtime, item)
    assert runtime.mcp.config["echo_service"].env["SERVICE_SECRET"] == "first-secret"
    monkeypatch.delenv("GENESIS_CAPABILITY_TEST_SECRET")
    runtime.refresh_capabilities()
    assert "echo_service" not in runtime.mcp.config
    assert runtime.capability_diagnostics
    runtime.refresh_capabilities()
    assert runtime.capability_diagnostics
    monkeypatch.setenv("GENESIS_CAPABILITY_TEST_SECRET", "second-secret")
    runtime.refresh_capabilities()
    assert runtime.mcp.config["echo_service"].env["SERVICE_SECRET"] == "second-secret"
    assert "second-secret" not in json.dumps(runtime.capabilities.inspect(item["id"]))


def test_discussion_mode_cannot_create_or_revise(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    runtime.store.record.run_runtime["constraints"] = {"read_only": True}
    _, changed = invoke(runtime, "revise_capability", {"id": item["id"], "expected_revision": 1, "changes": change_args(tool_package(), runtime.store.workspace)})
    assert changed["error"]["code"] == "task_constraint"
    package = tool_package("another_tool")
    _, result = invoke(runtime, "create_tool", draft_args(package, runtime.store.workspace))
    assert result["error"]["code"] == "task_constraint"
    assert len(runtime.capabilities.store.list()) == 1


def test_builder_discussion_can_continue_into_explicit_creation():
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    selector = TaskProtocolSelector()
    previous = selector.route("先讨论创建外挂工具的需求，暂时不要执行")
    assert previous["constraints"]["read_only"]
    route = selector.route("开始构建吧", previous=previous)
    assert route["protocols"] == ["capability_builder"]
    assert not route["constraints"]["read_only"]
    unrelated = selector.route("新任务，修复 Python 代码", previous=route)
    assert "capability_builder" not in unrelated["protocols"]


@pytest.mark.parametrize("name", ["connect_mcp", "read_mcp_resource", "mcp_other_echo"])
def test_bridge_namespace_cannot_be_occupied_by_external_tools(capability_runtime, name):
    package = tool_package(name)
    _, result = invoke(capability_runtime, "create_tool", draft_args(package, capability_runtime.store.workspace))
    assert result["error"]["code"] == "capability_invalid"


def test_scopes_cannot_publish_same_name_even_after_both_pass(capability_runtime):
    runtime = capability_runtime
    first = created(runtime, tool_package())
    second = created(runtime, tool_package(scope="user"))
    invoke(runtime, "test_capability", reference(first))
    _, tested = invoke(runtime, "test_capability", reference(second))
    assert tested["data"]["report"]["passed"]
    accept(runtime, first)
    _, rejected = invoke(runtime, "register_capability", reference(second))
    assert rejected["error"]["code"] == "capability_conflict"
    with pytest.raises(ToolError, match="同名能力"):
        runtime.capabilities.store.publish(second["id"], second["revision"], second["digest"], tested["data"]["report"]["id"], runtime.store.id)


def test_skill_mcp_dependency_uses_separate_connection(capability_runtime):
    from test_acceptance import FakeModel, call, response
    from genesisai.shared.messages import Response
    runtime = capability_runtime
    server = created(runtime, mcp_package())
    invoke(runtime, "test_capability", reference(server))
    accept(runtime, server)
    invoke(runtime, "connect_mcp", {"server": "echo_service"})
    cache = deepcopy(runtime.mcp.cache)
    assert not runtime.mcp.transports
    package = skill_package()
    package["definition"]["stages"]["run"]["required_tools"] = ["mcp_echo_service_echo"]
    package["files"]["SKILL.md"] = "调用 echo 服务发送 hello，报告真实返回文字。"
    package["tests"] = [{"name": "echo", "input": {"stage": "run", "prompt": "按照 Skill 调用服务"}, "expect": {"type": "object", "properties": {"answer": {"const": "hello"}}, "required": ["answer"]}}]
    skill = created(runtime, package)
    model = FakeModel(response(call("mcp_echo_service_echo", {"text": "hello"}, "skill_echo")), Response(content="hello"))
    runtime.bind_model(model)
    _, tested = invoke(runtime, "test_capability", reference(skill))
    assert tested["data"]["report"]["passed"], tested
    assert runtime.mcp.cache == cache and not runtime.mcp.transports
    trial_result = tested['data']['report']['cases'][0]['result']
    trial_session = Session(trial_result['state_root'], trial_result['session_id'])
    assert any('"text": "hello"' in str(message) for message in trial_session.record.messages)
    assert not model.contexts  # The durable worker owns an independent model instance.
    _, echoed = invoke(runtime, "mcp_echo_service_echo", {"text": "still live"})
    assert echoed["data"]["text"] == "still live"


def test_mcp_image_trial_has_separate_assets(capability_runtime):
    import base64
    import io
    from PIL import Image
    runtime = capability_runtime
    stream = io.BytesIO()
    Image.new("RGB", (2, 3), "red").save(stream, format="PNG")
    block = {"type": "image", "mimeType": "image/png", "data": base64.b64encode(stream.getvalue()).decode()}
    package = mcp_package()
    package["files"]["server.py"] = package["files"]["server.py"].replace("{'type': 'text', 'text': request['params']['arguments']['text']}", repr(block))
    package["tests"][0]["expect"] = {"type": "object", "properties": {"images": {"type": "array", "minItems": 1, "items": {"properties": {"width": {"const": 2}, "height": {"const": 3}}}}}, "required": ["images"]}
    item = created(runtime, package)
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"], json.dumps(tested["data"]["report"], ensure_ascii=False)
    report_dir = Path(tested["data"]["report"]["directory"])
    assert list(report_dir.rglob("*.png"))
    assert not runtime.store.record.sources


def test_registered_user_scope_reusable_in_another_project(capability_runtime, tmp_path):
    runtime = capability_runtime
    for package in (tool_package("project_double"), tool_package("user_double", scope="user")):
        item = created(runtime, package)
        invoke(runtime, "test_capability", reference(item))
        accept(runtime, item)
    workspace = tmp_path / "other_project"
    workspace.mkdir()
    session = Session(runtime.store.root, workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.commit()
    other = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), permission_mode="auto", providers=[])
    try:
        assert "project_double" not in other.registry
        assert "user_double" in other.registry
        _, result = invoke(other, "user_double", {"value": 12})
        assert result["ok"] and result["data"]["value"]["value"] == 24
        assert Path(result["data"]["value"]["cwd"]) == workspace
        assert not (runtime.store.workspace / "output.txt").exists()
    finally:
        other.close()


def test_capability_suite_detaches_and_collects_persisted_case_results(capability_runtime):
    runtime = capability_runtime
    package = tool_package('slow_double')
    package['files']['tool.py'] = 'import time; time.sleep(4)\n' + package['files']['tool.py']
    package['tests'] = [
        {'name': 'positive', 'input': {'value': 3}, 'expect': {'properties': {'value': {'const': 6}}, 'required': ['value']}},
        {'name': 'negative', 'input': {'value': -2}, 'expect': {'properties': {'value': {'const': -4}}, 'required': ['value']}},
    ]
    item = created(runtime, package)
    _, started = invoke(runtime, 'test_capability', reference(item))
    assert started['ok'], started
    job_id = started['data']['job_id']
    assert started['data']['status'] in {'queued', 'running'}
    assert runtime.capabilities.store.get(item['id'])['report'] is None
    runtime.close()
    restored = ToolRuntime(Session(runtime.store.root, runtime.store.id), runtime.access, permission_mode='auto', providers=[])
    try:
        _, finished = invoke(restored, 'read_job', {'job_id': job_id, 'wait_seconds': 20})
        assert finished['ok'], finished
        data = finished['data']
        assert data['status'] == 'succeeded' and data['result']['report']['passed']
        assert [case['name'] for case in data['result']['report']['cases']] == ['positive', 'negative']
        assert data['progress']['completed'] == data['progress']['total'] == 2
        assert job_id not in restored.store.record.run_runtime['pending_execution_jobs']
        assert restored.capabilities.store.get(item['id'])['report']['id'] == data['result']['report']['id']
    finally:
        restored.close()


def test_submitted_trial_remains_pending_when_foreground_observation_fails(capability_runtime, monkeypatch):
    runtime = capability_runtime
    item = created(runtime, tool_package())
    with monkeypatch.context() as patch:
        def lost_reply(*args, **kwargs):
            raise ToolError('unknown_execution', 'fixture foreground read failure')
        patch.setattr(runtime.jobs, 'wait', lost_reply)
        with pytest.raises(ToolError, match='foreground read failure'):
            runtime.capabilities.test(reference(item))
    restored = Session(runtime.store.root, runtime.store.id)
    job_id, = restored.record.run_runtime['pending_execution_jobs']
    assert job_id in runtime.completion_issue()
    assert runtime.jobs.wait(job_id, 15)['status'] == 'succeeded'
    result = runtime.jobs.operation_result(job_id)
    assert result['report']['passed']
    assert len(runtime.jobs.status()['jobs']) == 1
    assert Session(runtime.store.root, runtime.store.id).record.run_runtime['pending_execution_jobs'] == []


def test_unknown_operation_stays_pending_and_cannot_be_delivered_as_success(capability_runtime):
    from genesisai.core.jobs.storage import save
    runtime = capability_runtime
    job_id = 'job_unknown_fixture'
    root = runtime.jobs._path(job_id)
    root.mkdir(parents=True)
    save(root / 'job.json', {'job_id': job_id, 'kind': 'mcp_request', 'status': 'unknown',
                             'code': 'mcp_error', 'error': 'remote disconnected'})
    runtime.jobs._pending(job_id, True)
    with pytest.raises(ToolError) as error:
        runtime.jobs.operation_result(job_id)
    assert error.value.error_type == 'unknown_execution'
    assert job_id in Session(runtime.store.root, runtime.store.id).record.run_runtime['pending_execution_jobs']
