"""四字段工具 YAML 和受信任 Registry。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from genesisai.runtime.base import ToolSpec, object_schema
from genesisai.runtime.registry import CORE_NAMES, TOOL_FIELDS, RegistryError, ToolRegistry


def valid_spec(permission="read"):
    return ToolSpec(
        parameters=object_schema([], value={"type": "string"}),
        permission=permission,
        network=permission == "network",
    )


def write_tool(root, category, directory, data):
    path = root / category / directory
    path.mkdir(parents=True)
    (path / "tool.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (path / "spec.py").write_text("SPEC = None\n", encoding="utf-8")
    (path / "implementation.py").write_text("class Implementation: pass\n", encoding="utf-8")


def registry_from(root, monkeypatch):
    monkeypatch.setattr(
        "genesisai.runtime.registry.importlib.import_module",
        lambda name: SimpleNamespace(
            SPEC=valid_spec("core" if ".core." in name else "network" if ".web." in name else "read")
        ),
    )
    return ToolRegistry(root)


def descriptor(name, category="filesystem", enabled=True):
    return {
        "name": name,
        "enabled": enabled,
        "category": category,
        "description": "测试工具",
    }


def test_real_registry_has_exact_four_field_manifests():
    registry = ToolRegistry()
    manifests = sorted(
        manifest
        for root in registry.category_roots.values()
        for manifest in root.glob("*/tool.yaml")
    )

    assert len(registry) == len(manifests) >= 21
    assert set(CORE_NAMES) == {item.name for item in registry.values() if item.category == "core"}
    for manifest in manifests:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert set(data) == TOOL_FIELDS
        assert data["name"] == manifest.parent.name
        assert "version" not in data and "schema_version" not in data
    assert not any(root.joinpath("catalog.yaml").exists() for root in registry.category_roots.values())


@pytest.mark.parametrize(
    "data,match",
    [
        ({"name": "demo", "enabled": True, "category": "filesystem"}, "字段"),
        ({**descriptor("demo"), "version": "1"}, "字段"),
        ([], "根节点"),
        ({**descriptor("demo"), "enabled": "true"}, "布尔"),
        ({**descriptor("demo"), "category": "external"}, "category"),
    ],
)
def test_invalid_manifest_shapes_are_rejected(tmp_path, monkeypatch, data, match):
    write_tool(tmp_path, "filesystem", "demo", data)
    with pytest.raises(RegistryError, match=match):
        registry_from(tmp_path, monkeypatch)


def test_name_must_match_directory(tmp_path, monkeypatch):
    write_tool(tmp_path, "filesystem", "folder_name", descriptor("other_name"))
    with pytest.raises(RegistryError, match="目录不一致"):
        registry_from(tmp_path, monkeypatch)


def test_duplicate_name_is_rejected_across_categories(tmp_path, monkeypatch):
    write_tool(tmp_path, "filesystem", "duplicate", descriptor("duplicate"))
    write_tool(tmp_path, "web", "duplicate", descriptor("duplicate", "web"))
    with pytest.raises(RegistryError, match="重复"):
        registry_from(tmp_path, monkeypatch)


def test_core_cannot_be_disabled(tmp_path, monkeypatch):
    for name in CORE_NAMES:
        write_tool(tmp_path, "core", name, descriptor(name, "core", enabled=name != "tool_load"))
    with pytest.raises(RegistryError, match="Core"):
        registry_from(tmp_path, monkeypatch)


def test_yaml_python_constructor_is_not_executed(tmp_path, monkeypatch):
    path = tmp_path / "filesystem" / "unsafe"
    path.mkdir(parents=True)
    (path / "tool.yaml").write_text("!!python/object/apply:os.system ['echo unsafe']", encoding="utf-8")
    (path / "spec.py").write_text("SPEC = None\n", encoding="utf-8")
    (path / "implementation.py").write_text("class Implementation: pass\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="无法读取"):
        registry_from(tmp_path, monkeypatch)


def test_missing_spec_or_implementation_is_rejected(tmp_path, monkeypatch):
    write_tool(tmp_path, "filesystem", "demo", descriptor("demo"))
    (tmp_path / "filesystem" / "demo" / "implementation.py").unlink()
    with pytest.raises(RegistryError, match="缺少"):
        registry_from(tmp_path, monkeypatch)


def test_invalid_spec_is_rejected_before_runtime(tmp_path, monkeypatch):
    write_tool(tmp_path, "filesystem", "demo", descriptor("demo"))
    bad = ToolSpec(
        parameters={"type": "object", "properties": {}, "required": []},
        permission="read",
    )
    monkeypatch.setattr(
        "genesisai.runtime.registry.importlib.import_module",
        lambda name: SimpleNamespace(SPEC=bad),
    )

    with pytest.raises(RegistryError, match="Spec 无效"):
        ToolRegistry(tmp_path)


def test_registry_does_not_import_business_implementations():
    modules = [
        f"genesisai.capabilities.filesystem.{name}.implementation"
        for name in ("file_list", "file_search", "file_read", "file_create", "file_copy")
    ]
    modules += [
        f"genesisai.capabilities.web.{name}.implementation"
        for name in ("search_query", "search_fetch")
    ]
    for module in modules:
        sys.modules.pop(module, None)

    ToolRegistry()

    assert all(module not in sys.modules for module in modules)


def test_registry_root_is_inside_installed_genesisai_package():
    registry = ToolRegistry()
    package_root = Path(__file__).resolve().parents[1] / "src" / "genesisai"
    assert registry.root == package_root
    assert registry.category_roots["core"] == package_root / "runtime" / "builtin_tools" / "core"
    assert registry.category_roots["filesystem"] == package_root / "capabilities" / "filesystem"
    assert registry.category_roots["development"] == package_root / "capabilities" / "shell"


def test_registry_refresh_applies_yaml_enabled_change(tmp_path, monkeypatch):
    for name in CORE_NAMES:
        write_tool(tmp_path, "core", name, descriptor(name, "core"))
    write_tool(tmp_path, "filesystem", "file_read", descriptor("file_read"))
    registry = registry_from(tmp_path, monkeypatch)
    manifest = tmp_path / "filesystem" / "file_read" / "tool.yaml"
    manifest.write_text(
        yaml.safe_dump(descriptor("file_read", enabled=False), allow_unicode=True),
        encoding="utf-8",
    )

    registry.refresh()

    assert registry.get("file_read").enabled is False

