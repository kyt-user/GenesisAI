"""Catalog 四层集合、Core 工具与延迟加载。"""

import dataclasses
import json
import sys

import pytest

from genesisai.shared.security import Access, ToolError
from genesisai.state.store import Store
from genesisai.runtime.registry import CORE_NAMES, ToolRegistry
from genesisai.runtime.tool_runtime import ToolRuntime


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


def test_initial_definitions_are_exactly_core_discovery_tools(runtime):
    assert definition_names(runtime) == list(CORE_NAMES)
    snapshot = runtime.snapshot()
    assert len(snapshot["core"]) == len(CORE_NAMES) == 6
    assert len(snapshot["active"]) == 0
    assert len(snapshot["available"]) == len(runtime.registry) - len(CORE_NAMES)
    assert len(snapshot["disabled"]) == 0
    assert sum(map(len, snapshot.values())) == len(runtime.registry)


def test_search_is_compact_and_describe_does_not_activate(runtime):
    searched = runtime.execute(tool_call("tool_search", {"query": "文件", "category": "filesystem"}, "search"))
    assert searched["ok"]
    assert searched["data"]["tools"]
    assert all("parameters" not in item for item in searched["data"]["tools"])

    described = runtime.execute(tool_call("tool_describe", {"name": "file_read"}, "describe"))
    assert described["ok"]
    assert described["data"]["status"] == "available"
    assert described["data"]["parameters"]["required"] == ["path"]
    assert runtime.catalog.active_names == []


def test_load_is_atomic_idempotent_and_effective_in_next_definitions(runtime):
    loaded = runtime.execute(tool_call("tool_load", {"names": ["file_read", "file_list"]}, "load"))
    assert loaded["ok"]
    assert runtime.catalog.active_names == ["file_read", "file_list"]
    assert definition_names(runtime) == [*CORE_NAMES, "file_read", "file_list"]

    repeated = runtime.execute(tool_call("tool_load", {"names": ["file_read"]}, "repeat"))
    assert repeated["ok"]
    assert runtime.catalog.active_names == ["file_read", "file_list"]


def test_tool_load_can_atomically_replace_active_set(runtime):
    assert runtime.execute(tool_call("tool_load", {"names": ["file_read", "file_list"]}, "initial"))["ok"]
    result = runtime.execute(tool_call("tool_load", {"names": ["file_list", "git_status"], "replace": True}, "replace"))
    assert result["ok"]
    assert runtime.catalog.active_names == ["file_list", "git_status"]
    assert definition_names(runtime) == [*CORE_NAMES, "file_list", "git_status"]


def test_business_tool_cannot_execute_before_load(runtime):
    result = runtime.execute(tool_call("file_read", {"path": "note.md"}, "read_before"))
    assert not result["ok"]
    assert result["error"]["code"] == "tool_unavailable"


def test_unknown_and_active_limit_errors_leave_active_unchanged(runtime):
    unknown = runtime.execute(tool_call("tool_load", {"names": ["missing"]}, "unknown"))
    assert unknown["error"]["code"] == "unknown_tool"
    assert runtime.catalog.active_names == []

    limited = ToolRuntime(
        runtime.store,
        runtime.access,
        providers=[],
        confirm_writes=False,
        confirm_search=False,
        active_limit=1,
    )
    result = limited.execute(tool_call("tool_load", {"names": ["file_read", "file_list"]}, "limit"))
    assert result["error"]["code"] == "active_limit"
    assert limited.catalog.active_names == []


def test_loader_failure_rolls_back_cache_and_active_state(runtime, monkeypatch):
    original = runtime.loader.load

    def fail_second(descriptor):
        if descriptor.name == "file_list":
            raise ToolError("tool_load_failed", "fixture failure")
        return original(descriptor)

    monkeypatch.setattr(runtime.loader, "load", fail_second)
    result = runtime.execute(tool_call("tool_load", {"names": ["file_read", "file_list"]}, "load_fail"))

    assert result["error"]["code"] == "tool_load_failed"
    assert runtime.catalog.active_names == []
    assert not runtime.loader.loaded("file_read")


def test_persistence_failure_rolls_back_loaded_and_active_state(runtime, monkeypatch):
    real_save = runtime.store.save

    def fail_when_active():
        if runtime.store.data["tool_runtime"]["active_tools"]:
            raise OSError("fixture save failure")
        return real_save()

    monkeypatch.setattr(runtime.store, "save", fail_when_active)
    result = runtime.execute(tool_call("tool_load", {"names": ["file_read"]}, "save_fail"))

    assert result["error"]["code"] == "execution_error"
    assert runtime.catalog.active_names == []
    assert not runtime.loader.loaded("file_read")


def test_business_implementation_is_imported_only_when_loaded(runtime):
    module = "genesisai.capabilities.filesystem.file_read.implementation"
    sys.modules.pop(module, None)
    runtime.loader._cache.pop("file_read", None)
    assert module not in sys.modules

    runtime.execute(tool_call("tool_search", {"query": "file_read"}, "lazy_search"))
    assert module not in sys.modules
    runtime.execute(tool_call("tool_load", {"names": ["file_read"]}, "lazy_load"))

    assert module in sys.modules
    assert runtime.loader.loaded("file_read")


def test_disabled_tool_is_visible_but_cannot_load(tmp_path, monkeypatch):
    registry = ToolRegistry()
    registry._items["file_read"] = dataclasses.replace(registry.get("file_read"), enabled=False)
    inputs = tmp_path / "input"
    inputs.mkdir()
    store = Store(tmp_path / "work")
    output = tmp_path / "out"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), registry=registry, providers=[])
    monkeypatch.setattr(registry, "refresh", lambda: None)

    snapshot = runtime.snapshot()
    assert any(item["name"] == "file_read" for item in snapshot["disabled"])
    result = runtime.execute(tool_call("tool_load", {"names": ["file_read"]}, "disabled"))
    assert result["error"]["code"] == "tool_unavailable"


def test_unsupported_platform_tool_is_disabled(tmp_path, monkeypatch):
    registry = ToolRegistry()
    descriptor = registry.get("file_read")
    unsupported_spec = dataclasses.replace(descriptor.spec, platforms=("never-platform",))
    registry._items["file_read"] = dataclasses.replace(descriptor, spec=unsupported_spec)
    inputs = tmp_path / "input"
    inputs.mkdir()
    store = Store(tmp_path / "work")
    output = tmp_path / "out"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), registry=registry, providers=[])
    monkeypatch.setattr(registry, "refresh", lambda: None)

    result = runtime.execute(tool_call("tool_load", {"names": ["file_read"]}, "unsupported"))

    assert result["error"]["code"] == "tool_unavailable"
    assert "平台" in result["error"]["message"]

