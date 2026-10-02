"""七个业务工具与 Runtime 公共契约。"""

import json
from pathlib import Path

import pytest

from genesisai.shared.security import Access
from genesisai.state.store import Store
from genesisai.runtime.registry import CORE_NAMES
from genesisai.runtime.tool_runtime import ToolRuntime
import genesisai.runtime.executor as executor_module


BUSINESS = [
    "file_list",
    "file_search",
    "file_read",
    "file_create",
    "file_copy",
    "search_query",
    "search_fetch",
]
RESULT_FIELDS = {"call_id", "ok", "data", "error", "source_refs", "artifact_refs", "truncated"}


def tool_call(name, args, call_id):
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


@pytest.fixture
def runtime(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "note.md").write_text("needle", encoding="utf-8")
    (inputs / "needle.bin").write_bytes(b"\x00\x01")
    store = Store(tmp_path / "work")
    output = tmp_path / "output"
    store.data.update(grants=[str(inputs)], output=str(output))
    store.save()
    runtime = ToolRuntime(store, Access([inputs], output, store.root), providers=[], confirm_writes=False, confirm_search=False)
    runtime.load_tools(BUSINESS)
    return inputs, output, store, runtime


def test_business_specs_are_the_single_model_schema_source(runtime):
    expected_permissions = {
        "file_list": "read",
        "file_search": "read",
        "file_read": "read",
        "file_create": "write",
        "file_copy": "write",
        "search_query": "network",
        "search_fetch": "network",
    }
    definitions = {item["function"]["name"]: item["function"] for item in runtime[3].definitions()}

    assert list(definitions) == [*CORE_NAMES, *BUSINESS]
    for name in BUSINESS:
        descriptor = runtime[3].registry.get(name)
        assert definitions[name]["parameters"] is descriptor.spec.parameters
        assert descriptor.spec.permission == expected_permissions[name]


def test_success_and_failure_results_use_one_outer_shape(runtime):
    success = runtime[3].execute(tool_call("file_read", {"path": "note.md"}, "ok"))
    failure = runtime[3].execute(tool_call("file_read", {"path": "missing.md"}, "bad"))

    assert set(success) == set(failure) == RESULT_FIELDS
    assert success["ok"] and success["error"] is None
    assert not failure["ok"] and set(failure["error"]) == {"code", "message", "retryable"}


def test_file_search_matches_filename_even_if_content_is_unsupported(runtime):
    result = runtime[3].execute(
        tool_call("file_search", {"path": ".", "query": "needle", "content": True}, "search")
    )
    names = {Path(item["path"]).name for item in result["data"]["files"]}

    assert {"note.md", "needle.bin"}.issubset(names)
    assert any(Path(path).name == "needle.bin" for path in result["data"]["skipped"])


def test_file_listing_stops_at_repository_scan_cap(runtime):
    for index in range(1000):
        (runtime[0] / f"bulk-{index:04}.txt").write_text("x", encoding="utf-8")
    result = runtime[3].execute(tool_call("file_list", {"path": ".", "offset": 999}, "scan_cap"))

    assert result["ok"]
    assert result["truncated"]
    assert len(result["data"]["files"]) == 1
    assert result["data"]["next_offset"] is None


def test_file_copy_rejects_directory_source(runtime):
    (runtime[0] / "folder").mkdir()
    result = runtime[3].execute(
        tool_call("file_copy", {"source": "folder", "path": "folder-copy"}, "copy_dir")
    )

    assert result["error"]["code"] == "invalid_source"
    assert not (runtime[1] / "folder-copy").exists()


def test_search_query_caps_provider_results_at_five(runtime):
    class Provider:
        name = "many"

        def search(self, query, limit=5):
            return [{"title": str(index), "url": f"https://example.com/{index}"} for index in range(10)]

    runtime[3].providers = [Provider()]
    result = runtime[3].execute(tool_call("search_query", {"query": "bounded"}, "many"))

    assert result["ok"]
    assert len(result["data"]["hits"]) == 5


def test_unexpected_implementation_error_does_not_expose_message(runtime):
    class Broken:
        def prepare(self, context, args):
            return None

        def execute(self, context, args):
            raise RuntimeError("API_KEY=private-secret")

    runtime[3].loader._cache["file_read"] = Broken()
    result = runtime[3].execute(tool_call("file_read", {"path": "note.md"}, "secret"))

    assert result["error"]["code"] == "execution_error"
    assert "private-secret" not in result["error"]["message"]


def test_executor_has_no_business_dispatch_and_tools_do_not_call_models():
    root = Path(__file__).resolve().parents[1] / "src" / "genesisai"
    executor = (root / "runtime" / "executor.py").read_text(encoding="utf-8")
    assert {"DEFIN" + "ITIONS"}.isdisjoint(vars(executor_module))
    assert not any(f'call["name"] == "{name}"' in executor for name in BUSINESS)
    for implementation in (root / "capabilities").glob("*/*/implementation.py"):
        text = implementation.read_text(encoding="utf-8")
        assert ".chat(" not in text and "build_model" not in text

