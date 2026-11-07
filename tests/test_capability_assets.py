import json
from copy import deepcopy

import pytest

from genesisai.shared.security import ToolError
from test_capability_lifecycle import (capability_runtime, tool_package, created, invoke, reference, freeze, change_args)


def test_large_directory_snapshot_and_binary_fixtures_are_frozen(capability_runtime):
    runtime = capability_runtime
    service = runtime.capabilities
    item = service.create("tool", {"name": "binary_checker", "description": "Read a binary fixture"})
    with pytest.raises(ToolError, match="冻结"):
        service.preview_test(reference(item))
    with pytest.raises(ToolError):
        service.freeze({"id": item["id"], "expected_revision": 1})
    root = runtime.store.workspace / "implementation"
    root.mkdir()
    (root / "main.py").write_text("import json,os\nfrom pathlib import Path\nPath(os.environ['GENESISAI_RESULT_PATH']).write_text(json.dumps({'size':len(Path('data.bin').read_bytes()),'first':Path('data.bin').read_bytes()[0]}),encoding='utf-8')", encoding="utf-8")
    for number in range(105):
        (root / f"part_{number}.txt").write_text(str(number), encoding="utf-8")
    (root / "large.bin").write_bytes(b"\0" * 2_100_000)
    fixture = runtime.store.workspace / "fixture.bin"
    fixture.write_bytes(bytes(range(256)))
    case = {"name": "binary", "input": {}, "fixtures": {"data.bin": {"path": str(fixture)}}, "expect": {"type": "object", "properties": {"size": {"const": 256}, "first": {"const": 0}}, "required": ["size", "first"]}}
    definition = {"runner": "command", "command": ["{python}", "-B", "{bundle}/main.py"], "parameters": {"type": "object", "additionalProperties": False}, "output_schema": {"type": "object"}}
    item = service.revise({"id": item["id"], "expected_revision": 1, "changes": {"directory": str(root), "definition": definition, "tests": [case]}})
    assert item["revision"] == 2 and len(item["package"]["files"]) == 107
    assert len(json.dumps(item)) < 25_000  # The multi-MB payload is not a tool argument/result.
    (root / "large.bin").write_bytes(b"changed authoring source")
    item = service.freeze({"id": item["id"], "expected_revision": 2})
    bundle = service.store.materialize(service.store.get(item["id"]))
    assert (bundle / "large.bin").stat().st_size == 2_100_000
    tested = service.test(reference(item))
    assert tested["report"]["passed"], tested
    assert not (runtime.store.workspace / "data.bin").exists()


def test_incremental_revision_preserves_assets_invalidates_test_and_rejects_stale(capability_runtime):
    service = capability_runtime.capabilities
    item = created(capability_runtime, tool_package())
    tested = service.test(reference(item))
    assert tested["report"]["passed"]
    before = service.store.get(item["id"])["package"]
    changed = service.revise({"id": item["id"], "expected_revision": 1, "changes": {"description": "Updated purpose", "tests": [{"name": "double", "input": {"value": 4}, "expect": {"type": "object", "properties": {"value": {"const": 8}}, "required": ["value"]}}]}})
    assert changed["package"]["files"] == before["files"]
    assert len(changed["package"]["tests"]) == 2
    assert changed["report"] is None and not changed["frozen"]
    with pytest.raises(ToolError, match="修改"):
        service.revise({"id": item["id"], "expected_revision": 1, "changes": {"description": "stale"}})
    with pytest.raises(ToolError, match="冻结"):
        service.test(reference(changed))
    frozen = service.freeze({"id": item["id"], "expected_revision": 2})
    assert service.test(reference(frozen))["report"]["passed"]
    removed = service.revise({"id": item["id"], "expected_revision": 2, "changes": {"files": {"tool.py": None}}})
    assert not removed["package"]["files"]
    assert service.store.assets.resolve(before["files"]["tool.py"]).is_file()
    assert service.store.get(item["id"], 1)["report"]["passed"]


def test_more_than_twenty_cases_and_storage_quota(capability_runtime):
    runtime = capability_runtime
    package = tool_package()
    package["tests"] = [{**deepcopy(package["tests"][0]), "name": f"case_{i}"} for i in range(24)]
    item = created(runtime, package)
    from test_tool_result_jobs import finish
    tested = runtime.capabilities.test(reference(item))
    report = finish(runtime, {'ok': True, 'data': tested})["report"]
    assert report["passed"] and len(report["cases"]) == 24
    assets = runtime.capabilities.store.assets
    old = set(assets.root.iterdir())
    assets.limits["storage_max_bytes"] = 1
    source = runtime.store.workspace / "new.bin"
    source.write_bytes(b"brand new input")
    with pytest.raises(ToolError, match="配额"):
        runtime.capabilities.revise({"id": item["id"], "expected_revision": 1, "changes": {"files": {"new.bin": {"path": str(source)}}}})
    assert set(assets.root.iterdir()) == old and source.read_bytes() == b"brand new input"
    assert runtime.capabilities.store.get(item["id"])["revision"] == 1


def test_inline_files_and_full_replacement_interface_are_rejected(capability_runtime):
    runtime = capability_runtime
    _, result = invoke(runtime, "create_tool", {"name": "bad_inline", "description": "inline", "files": {"tool.py": "print('old')"}})
    assert not result["ok"]
    item = created(runtime, tool_package())
    _, result = invoke(runtime, "revise_capability", {"id": item["id"], "expected_revision": 1, "package": tool_package()})
    assert not result["ok"]
    assert runtime.capabilities.store.get(item["id"])["revision"] == 1


def test_corrupted_asset_cannot_be_frozen(capability_runtime):
    runtime = capability_runtime
    item = runtime.capabilities.create("tool", {"name": "corruption_probe", "description": "Check asset bytes"})
    changed = runtime.capabilities.revise({"id": item["id"], "expected_revision": 1, "changes": change_args(tool_package(), runtime.store.workspace)})
    asset = changed["package"]["files"]["tool.py"]
    runtime.capabilities.store.assets.resolve(asset).write_bytes(b"tampered")
    with pytest.raises(ToolError, match="哈希"):
        runtime.capabilities.freeze({"id": item["id"], "expected_revision": 2})
