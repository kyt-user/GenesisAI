import dataclasses
import json
import sys

import pytest

from genesisai.shared.security import Access
from genesisai.core.persistence.session import Session
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.tools.tool_runtime import ToolRuntime


def tool_call(name, args, call_id):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


@pytest.fixture
def runtime(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "note.md").write_text("catalog", encoding="utf-8")
    store = Session(tmp_path / "work")
    output = tmp_path / "output"
    store.record.apply(grants=[str(inputs)], output=str(output))
    store.commit()
    return ToolRuntime(store, Access([inputs], output, store.root), providers=[], permission_mode="auto")


def definition_names(runtime):
    return [item["function"]["name"] for item in runtime.definitions()]


def test_definitions_select_a_subset_of_enabled_and_available_tools(runtime):
    inventory = runtime.inventory()
    entries = [item for group in inventory.values() for item in group]
    enabled = {item["name"] for item in entries if item["enabled"]}

    assert {"discover_tools", "attempt_completion"} <= set(definition_names(runtime)) < enabled
    assert "read_files" in enabled and "editor" in enabled
    assert all(item.get("reason") for item in entries if not item["enabled"])
    assert enabled | {item["name"] for item in entries if not item["enabled"]} == set(runtime.registry._items)
    assert set(inventory) == {"builtin", "external"}


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
    store = Session(tmp_path / "work")
    output = tmp_path / "out"
    store.record.apply(grants=[str(inputs)], output=str(output))
    store.commit()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), registry=registry, providers=[])
    monkeypatch.setattr(registry, "refresh", lambda: None)

    inventory = runtime.inventory()
    assert any(item["name"] == "read_files" and not item["enabled"] for item in inventory["builtin"])
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
    store = Session(tmp_path / "work")
    output = tmp_path / "out"
    store.record.apply(grants=[str(inputs)], output=str(output))
    store.commit()
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
    assert 'active_tools' not in getattr(runtime.store.record, 'tool_runtime', {})
    assert not hasattr(runtime, "load_tools") and not hasattr(runtime, "reset_task")


def test_discovery_pages_filter_and_exact_schema_loading(runtime):
    expected = {item.name for item in runtime.catalog.available_tools() if item.category == 'kernel' and item.source == 'builtin'}
    names, offset = [], 0
    while True:
        page = runtime.discover_tools(limit=7, offset=offset, category='kernel', source='builtin', activate=False)
        names.extend(item['name'] for item in page['candidates'])
        assert all(item['category'] == 'kernel' and item['source'] == 'builtin' for item in page['candidates'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert len(names) == len(set(names)) and set(names) == expected
    assert runtime.discover_tools(provider='missing-provider')['candidates'] == []
    selected = runtime.discover_tools(query='read_files', limit=1)
    assert selected['available_next_round'][0]['name'] == 'read_files'
    assert 'read_files' in definition_names(runtime)
