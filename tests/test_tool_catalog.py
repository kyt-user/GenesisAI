import dataclasses
import json
import sys

import pytest

from genesisai.shared.security import Access
from genesisai.core.state.store import Store
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.tools.tool_runtime import ToolRuntime


def tool_call(name, args, call_id):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


@pytest.fixture
def runtime(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "note.md").write_text("catalog", encoding="utf-8")
    store = Store(tmp_path / "work")
    output = tmp_path / "output"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    return ToolRuntime(store, Access([inputs], output, store.root), providers=[], confirm_writes=False, confirm_search=False)


def definition_names(runtime):
    return [item["function"]["name"] for item in runtime.definitions()]


def test_definitions_are_all_enabled_and_available_tools(runtime):
    inventory = runtime.inventory()
    enabled = {item["name"] for item in inventory["enabled"]}

    assert set(definition_names(runtime)) == enabled
    assert "read_files" in enabled and "editor" in enabled
    assert all(item.get("reason") for item in inventory["disabled"])
    assert enabled | {item["name"] for item in inventory["disabled"]} == set(runtime.registry._items)


def test_business_tool_executes_without_any_load(runtime):
    result = runtime.execute(tool_call("read_files", {"path": "note.md"}, "read"))

    assert result["ok"]
    assert "catalog" in result["data"]["files"][0]["text"]


def test_unknown_tool_is_rejected(runtime):
    result = runtime.execute(tool_call("missing_tool", {}, "missing"))

    assert not result["ok"]
    assert result["error"]["code"] == "unknown_tool"


def test_disabled_tool_is_listed_and_cannot_execute(tmp_path, monkeypatch):
    registry = ToolRegistry()
    registry._items["read_files"] = dataclasses.replace(registry.get("read_files"), enabled=False)
    inputs = tmp_path / "input"
    inputs.mkdir()
    store = Store(tmp_path / "work")
    output = tmp_path / "out"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), registry=registry, providers=[])
    monkeypatch.setattr(registry, "refresh", lambda: None)

    inventory = runtime.inventory()
    assert any(item["name"] == "read_files" for item in inventory["disabled"])
    assert "read_files" not in definition_names(runtime)
    result = runtime.execute(tool_call("read_files", {"path": "note.md"}, "disabled"))
    assert result["error"]["code"] == "tool_unavailable"


def test_unsupported_platform_tool_is_disabled(tmp_path, monkeypatch):
    registry = ToolRegistry()
    descriptor = registry.get("read_files")
    unsupported_spec = dataclasses.replace(descriptor.spec, platforms=("never-platform",))
    registry._items["read_files"] = dataclasses.replace(descriptor, spec=unsupported_spec)
    inputs = tmp_path / "input"
    inputs.mkdir()
    store = Store(tmp_path / "work")
    output = tmp_path / "out"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), registry=registry, providers=[])
    monkeypatch.setattr(registry, "refresh", lambda: None)

    result = runtime.execute(tool_call("read_files", {"path": "note.md"}, "unsupported"))

    assert result["error"]["code"] == "tool_unavailable"
    assert "平台" in result["error"]["message"]


def test_definitions_do_not_import_implementations(runtime):
    module = "genesisai.core.extensions.tools.kernel.read_files.implementation"
    sys.modules.pop(module, None)
    runtime.loader._cache.pop("read_files", None)

    runtime.definitions()
    assert module not in sys.modules

    runtime.execute(tool_call("read_files", {"path": "note.md"}, "lazy"))
    assert module in sys.modules


def test_on_demand_loading_meta_tools_are_gone(runtime):
    """规格 §9.4：6 个按需加载元工具与 active_tools 字段必须彻底消失。"""
    meta = {"tool_search", "tool_describe", "tool_load", "skill_search", "skill_describe", "skill_load"}

    assert meta.isdisjoint(set(runtime.registry._items))
    assert meta.isdisjoint(definition_names(runtime))
    assert "active_tools" not in runtime.store.data.get("tool_runtime", {})
    assert not hasattr(runtime, "load_tools") and not hasattr(runtime, "reset_task")