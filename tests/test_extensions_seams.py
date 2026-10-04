"""S5 外部扩展缝契约测试：Skills / Rules / MCP / Workflows。"""

import json

import pytest

from genesisai.shared.security import Access
from genesisai.core.state.store import Store
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.core.extensions.rules import RulesLoader
from genesisai.core.extensions.workflows import WorkflowError, WorkflowRegistry
from genesisai.core.extensions.mcp import McpBridge, McpError, load_servers


def tool_call(name, args, call_id="c"):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def build_runtime(workspace):
    inputs = workspace / "input"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "note.md").write_text("hello", encoding="utf-8")
    store = Store(workspace / "state", workspace=workspace)
    output = workspace / "output"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    return ToolRuntime(
        store,
        Access([inputs], output, store.root, workspace_root=workspace),
        providers=[],
        confirm_writes=False,
        confirm_search=False,
        confirm_shell=False,
    )


# --- Skills 缝 -------------------------------------------------------------


def test_use_skill_returns_instruction_body(tmp_path):
    runtime = build_runtime(tmp_path)

    result = runtime.execute(tool_call("use_skill", {"name": "bug_fix"}, "skill"))

    assert result["ok"]
    assert result["data"]["name"] == "bug_fix"
    assert result["data"]["content"].strip()
    assert "read_files" in result["data"]["tools"]
    assert runtime.registry.get("use_skill").category == "kernel"


def test_use_skill_unknown_is_rejected(tmp_path):
    runtime = build_runtime(tmp_path)

    result = runtime.execute(tool_call("use_skill", {"name": "not_a_skill"}, "missing"))

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

    assert "只读规则：禁止改写密钥" in context[0].content


def test_rules_absent_is_noop(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert RulesLoader(workspace, home=tmp_path / "none").inject() is None


# --- Workflows 缝 ----------------------------------------------------------


def test_workflows_scan_and_expand(tmp_path):
    workspace = tmp_path / "ws"
    root = workspace / ".genesis" / "workflows"
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
        if method == "initialize":
            return {"protocolVersion": "2024-11-05"}
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


def write_mcp_config(workspace, *, read_only=True):
    (workspace / ".genesis").mkdir(exist_ok=True)
    (workspace / ".genesis" / "mcp.json").write_text(
        json.dumps({"servers": {"fixture": {"command": "noop", "read_only": read_only}}}),
        encoding="utf-8",
    )


def test_mcp_registers_and_executes_through_pipeline(tmp_path):
    runtime = build_runtime(tmp_path)
    write_mcp_config(tmp_path)
    transports = []

    def factory(spec):
        transport = FakeTransport(spec)
        transports.append(transport)
        return transport

    bridge = McpBridge(tmp_path, runtime.registry, runtime.loader, transport_factory=factory)
    bridge.connect_all()

    names = {item["function"]["name"] for item in runtime.definitions()}
    assert "mcp_fixture_echo" in names
    descriptor = runtime.registry.get("mcp_fixture_echo")
    assert descriptor.category == "mcp"
    assert descriptor.spec.permission == "read"

    result = runtime.execute(tool_call("mcp_fixture_echo", {"text": "hi"}, "mcp"))
    assert result["ok"]
    assert result["data"]["text"] == "echo:hi"

    # refresh（每次下发都会触发）必须保留动态注册。
    runtime.catalog.refresh()
    assert "mcp_fixture_echo" in {item["function"]["name"] for item in runtime.definitions()}


def test_mcp_write_server_maps_permission(tmp_path):
    runtime = build_runtime(tmp_path)
    write_mcp_config(tmp_path, read_only=False)

    bridge = McpBridge(
        tmp_path, runtime.registry, runtime.loader, transport_factory=lambda spec: FakeTransport(spec)
    )
    bridge.connect_all()

    descriptor = runtime.registry.get("mcp_fixture_echo")
    assert descriptor.spec.permission == "write"
    assert descriptor.spec.side_effect is True


def test_mcp_invalid_config_is_reported(tmp_path):
    runtime = build_runtime(tmp_path)
    (tmp_path / ".genesis").mkdir(exist_ok=True)
    (tmp_path / ".genesis" / "mcp.json").write_text("{}", encoding="utf-8")

    with pytest.raises(McpError):
        load_servers(tmp_path)
    bridge = McpBridge(
        tmp_path, runtime.registry, runtime.loader, transport_factory=lambda spec: FakeTransport(spec)
    )
    bridge.connect_all()
    assert bridge.servers == []
    assert bridge.diagnostics


def test_mcp_connection_failure_is_isolated(tmp_path):
    runtime = build_runtime(tmp_path)
    write_mcp_config(tmp_path)

    bridge = McpBridge(
        tmp_path,
        runtime.registry,
        runtime.loader,
        transport_factory=lambda spec: FakeTransport(spec, fail=True),
    )
    bridge.connect_all()

    assert bridge.diagnostics and bridge.servers == []
    assert "mcp_fixture_echo" not in {item["function"]["name"] for item in runtime.definitions()}