"""S5 外部扩展缝契约测试：Skills / Rules / MCP / Workflows。"""

import json

import pytest

from genesisai.shared.security import Access
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.core.extensions.rules import RulesLoader
from genesisai.core.extensions.workflows import WorkflowError, WorkflowRegistry
from genesisai.core.extensions.mcp import McpBridge, McpError, McpServerSpec


def tool_call(name, args, call_id="c"):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def build_runtime(workspace):
    inputs = workspace / "input"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "note.md").write_text("hello", encoding="utf-8")
    store = Session(workspace / "state", workspace=workspace)
    output = workspace / "output"
    store.record.apply(grants=[str(inputs)], output=str(output))
    store.commit()
    return ToolRuntime(
        store,
        Access([inputs], output, store.root, workspace_root=workspace),
        providers=[],
        permission_mode="auto",
    )


# --- Skills 缝 -------------------------------------------------------------


def test_use_skill_returns_instruction_body(tmp_path):
    runtime = build_runtime(tmp_path)

    result = runtime.execute(tool_call("use_skill", {"name": "bug_fix", "stage": "run"}, "skill"))

    assert result["ok"]
    assert result["data"]["name"] == "bug_fix"
    assert result["data"]["content"].strip()
    assert "read_files" in result["data"]["tools"]
    assert runtime.registry.get("use_skill").category == "kernel"
    assert runtime.store.record.tool_runtime["active_skills"] == {"bug_fix": "run"}
    removed = runtime.execute(tool_call("use_skill", {"name": "bug_fix", "stage": "run", "operation": "unload"}, "unload"))
    assert removed["ok"] and not runtime.store.record.tool_runtime["active_skills"]


def test_more_than_two_skills_survive_persistence_and_context(tmp_path):
    from dataclasses import replace
    runtime = build_runtime(tmp_path)
    template = runtime.skills.items["bug_fix"]
    names = ["workflow_" + str(index) for index in range(6)]
    for name in names:
        runtime.skills.items[name] = replace(template, name=name)
    runtime.store.record.tool_runtime["active_skills"] = {name: "run" for name in names}
    runtime.store.commit()
    restored = Session(runtime.store.root, runtime.store.id)
    assert restored.record.tool_runtime["active_skills"] == {name: "run" for name in names}
    from genesisai.core.prompt.context_sources import skills_context
    assert [block.source for block in skills_context({"runtime": runtime, "session": restored})] == ["skill:" + name for name in names]


def test_use_skill_unknown_is_rejected(tmp_path):
    runtime = build_runtime(tmp_path)

    result = runtime.execute(tool_call("use_skill", {"name": "not_a_skill", "stage": "run"}, "missing"))

    assert not result["ok"]
    assert result["error"]["code"] == "unknown_skill"


# --- Rules 缝 --------------------------------------------------------------


def test_rules_loads_project_and_user_and_redacts(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("项目规则\napi_key=SECRETVALUE\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".genesisai").mkdir(parents=True)
    (home / ".genesisai" / "AGENTS.md").write_text("用户规则：保持简洁", encoding="utf-8")

    loader = RulesLoader(workspace, home=home)
    blocks = loader.load()

    assert {block["source"] for block in blocks} == {"project", "user"}
    rendered = json.dumps(blocks, ensure_ascii=False)
    assert "SECRETVALUE" not in rendered
    assert "[REDACTED]" in rendered
    injected = loader.inject()
    assert "项目规则" in injected and "用户规则" in injected


def test_rules_injected_into_system_prompt(tmp_path):
    runtime = build_runtime(tmp_path)
    (tmp_path / "AGENTS.md").write_text("只读规则：禁止改写密钥", encoding="utf-8")

    context = runtime.context_budgeter.build("local_files")

    assert any(m.role=="user" and "只读规则：禁止改写密钥" in str(m.content) for m in context)


def test_rules_absent_is_noop(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert RulesLoader(workspace, home=tmp_path / "none").inject() is None


def test_long_rules_are_complete_and_directory_rules_are_scoped(tmp_path):
    nested = tmp_path / "src" / "module"
    nested.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "AGENTS.md").write_text("root" + "x" * 9000 + "TAIL", encoding="utf-8")
    (tmp_path / "src" / "AGENTS.md").write_text("source rules", encoding="utf-8")
    (nested / "AGENTS.md").write_text("module rules", encoding="utf-8")
    (other / "AGENTS.md").write_text("not in scope", encoding="utf-8")
    blocks = RulesLoader(tmp_path, home=tmp_path / "none").load([nested / "new.py"])
    assert len(blocks) == 3 and blocks[0]["text"].endswith("TAIL")
    assert blocks[-1]["text"] == "module rules"
    assert "not in scope" not in str(blocks)


# --- Workflows 缝 ----------------------------------------------------------


def test_workflows_scan_and_expand(tmp_path):
    workspace = tmp_path / "ws"
    root = workspace / "workflows"
    root.mkdir(parents=True)
    (root / "release.md").write_text("发布检查清单", encoding="utf-8")
    (root / "Bad Name.md").write_text("x", encoding="utf-8")

    registry = WorkflowRegistry(workspace)

    assert registry.names() == ["release"]
    assert registry.expand("release") == "发布检查清单"
    assert registry.diagnostics
    with pytest.raises(WorkflowError):
        registry.expand("missing")


def test_workflows_missing_directory_is_empty(tmp_path):
    registry = WorkflowRegistry(tmp_path)
    assert registry.names() == []
    assert registry.diagnostics == []


def test_long_workflow_has_no_silent_tail_truncation(tmp_path):
    (tmp_path / "workflows").mkdir()
    text = "x" * 15000 + "FINAL ACCEPTANCE"
    (tmp_path / "workflows" / "release.md").write_text(text, encoding="utf-8")
    assert WorkflowRegistry(tmp_path).expand("release") == text


# --- MCP 缝 ----------------------------------------------------------------


class FakeTransport:
    """内存版 MCP 传输，用于在不启动进程的情况下验证桥接契约。"""

    def __init__(self, spec, *, fail=False):
        self.spec = spec
        self.fail = fail
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise McpError("boom")
        if method == "server/discover":
            return {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "echo",
                        "description": "echo the provided text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "echo:" + str(params["arguments"].get("text"))}]}
        return {}

    def notify(self, method, params):
        self.calls.append((method, params))

    def close(self):
        pass


def fixture_servers(*, read_only=True):
    return [McpServerSpec("fixture", "stdio", "noop", read_only=read_only)]


def test_mcp_registers_and_executes_through_pipeline(tmp_path):
    runtime = build_runtime(tmp_path)
    marker = tmp_path / 'transports.txt'

    def factory(spec):
        transport = FakeTransport(spec)
        with marker.open('a', encoding='utf-8') as stream:
            stream.write(spec.name + '\n')
        return transport

    bridge = McpBridge(tmp_path, runtime.registry, runtime.loader, transport_factory=factory)
    bridge.configure({}, servers=fixture_servers())
    runtime.set_permission("user")
    connect = tool_call("connect_mcp", {"server": "fixture"}, "connect")
    assert runtime.execute(connect)["pending"] and not marker.exists()
    assert runtime.execute(connect, approved=runtime.store.record.calls['connect']['digest'])['ok']
    assert not bridge.transports

    assert "mcp_fixture_echo" not in {item["function"]["name"] for item in runtime.definitions()}
    runtime.discover_tools("mcp_fixture_echo", 1)
    names = {item["function"]["name"] for item in runtime.definitions()}
    assert "mcp_fixture_echo" in names
    descriptor = runtime.registry.get("mcp_fixture_echo")
    assert descriptor.category == "mcp"
    assert descriptor.spec.permission == "network"

    call = tool_call("mcp_fixture_echo", {"text": "hi"}, "mcp")
    assert runtime.execute(call)["pending"]
    result = runtime.execute(call, approved=runtime.store.record.calls['mcp']['digest'])
    assert result["ok"]
    assert result["data"]["text"] == "echo:hi"

    pending = tool_call("mcp_fixture_echo", {"text": "again"}, "mcp2")
    assert runtime.execute(pending)["pending"]
    bridge.close()
    assert not bridge.transports
    assert runtime.execute(pending, approved=runtime.store.record.calls['mcp2']['digest'])['ok']
    assert len(marker.read_text(encoding='utf-8').splitlines()) == 3  # Discovery and each call use independent hosts.

    # refresh（每次下发都会触发）必须保留动态注册。
    runtime.catalog.refresh()
    assert "mcp_fixture_echo" in {item["function"]["name"] for item in runtime.definitions()}


def test_mcp_write_server_maps_permission(tmp_path):
    runtime = build_runtime(tmp_path)

    bridge = McpBridge(
        tmp_path, runtime.registry, runtime.loader, transport_factory=lambda spec: FakeTransport(spec)
    )
    bridge.configure({}, servers=fixture_servers(read_only=False))
    bridge.ensure_connected("fixture")

    descriptor = runtime.registry.get("mcp_fixture_echo")
    assert descriptor.spec.permission == "network"
    assert descriptor.spec.side_effect is True


def test_mcp_definitions_are_explicit_and_invalid_specs_are_rejected(tmp_path):
    runtime = build_runtime(tmp_path)
    (tmp_path / ".genesis").mkdir(exist_ok=True)
    (tmp_path / "mcp.json").write_text("{}", encoding="utf-8")

    bridge = McpBridge(
        tmp_path, runtime.registry, runtime.loader, transport_factory=lambda spec: FakeTransport(spec)
    )
    bridge.configure({}, servers=[])
    assert bridge.servers == []
    assert "connect_mcp" not in runtime.registry
    with pytest.raises(McpError):
        bridge.configure({}, servers=[{}])


def test_mcp_read_only_requires_json_boolean(tmp_path):
    with pytest.raises(McpError, match="read_only"):
        fixture_servers(read_only="false")


def test_mcp_subprocess_gets_only_explicit_secret_env(monkeypatch):
    import sys
    from genesisai.core.extensions.mcp.bridge import McpServerSpec, StdioTransport

    monkeypatch.setenv("PARENT_API_KEY", "parent")
    command = (
        "import json,os; input(); "
        "print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'resultType':'complete','values':[os.getenv('PARENT_API_KEY'),os.getenv('MCP_TOKEN')]}}))"
    )
    spec = McpServerSpec("fixture", "stdio", sys.executable, ("-c", command), {"MCP_TOKEN": "explicit"})
    transport = StdioTransport(spec)
    try:
        assert transport.request("probe", {})["values"] == [None, "explicit"]
    finally:
        transport.close()


@pytest.mark.parametrize("invalid_tool", [
    {"name": "echo", "inputSchema": {"type": "object"}},
    {"name": "broken", "inputSchema": {"type": "object", "properties": []}},
])
def test_mcp_catalog_validation_precedes_registration(tmp_path, invalid_tool):
    class BrokenCatalog(FakeTransport):
        def request(self, method, params):
            result = super().request(method, params)
            if method == "tools/list":
                result["tools"].append(invalid_tool)
            return result

    runtime = build_runtime(tmp_path)
    bridge = McpBridge(tmp_path, runtime.registry, runtime.loader, transport_factory=BrokenCatalog)
    cache = {}
    bridge.configure(cache, servers=fixture_servers())
    with pytest.raises(McpError):
        bridge.ensure_connected("fixture")
    assert not cache and not bridge.transports
    assert "mcp_fixture_echo" not in runtime.registry
    assert not runtime.loader.loaded("mcp_fixture_echo")


def test_mcp_connection_failure_is_isolated(tmp_path):
    runtime = build_runtime(tmp_path)

    bridge = McpBridge(
        tmp_path,
        runtime.registry,
        runtime.loader,
        transport_factory=lambda spec: FakeTransport(spec, fail=True),
    )
    bridge.configure({}, servers=fixture_servers())
    with pytest.raises(McpError):
        bridge.ensure_connected("fixture")

    assert bridge.servers == []
    assert "mcp_fixture_echo" not in {item["function"]["name"] for item in runtime.definitions()}
