import json
import time

import pytest

from genesisai.runtime.tool_runtime import ToolRuntime
from genesisai.shared.security import Access
from genesisai.state.store import Store


@pytest.fixture
def source_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(workspace / ".genesis", workspace=workspace)
    store.data.update(
        grants=[str(workspace)],
        output=str(workspace),
        deadline=time.time() + 300,
    )
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        confirm_writes=False,
        confirm_search=False,
        providers=[],
    )
    runtime.load_tools(["file_create"])
    return workspace, store, runtime


def create(runtime, path, content, call_id="create"):
    return runtime.execute({
        "id": call_id,
        "name": "file_create",
        "arguments": json.dumps({"path": path, "content": content, "source_refs": []}),
    })


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("main.py", "src_index = 1\nprint(src_index)\n"),
        ("src/Main.java", "public class Main { public static void main(String[] args) {} }\n"),
        ("index.html", "<!doctype html><html><body>ok</body></html>\n"),
        ("app.css", "body { color: #123; }\n"),
        ("app.js", "const answer = 42;\n"),
        ("pyproject.toml", "[project]\nname = \"demo\"\n"),
        ("config.yaml", "enabled: true\n"),
    ],
)
def test_source_and_config_files_are_created_verbatim_and_recorded(source_runtime, path, content):
    workspace, store, runtime = source_runtime

    result = create(runtime, path, content, call_id="create_" + path.replace("/", "_"))

    assert result["ok"]
    assert result["data"]["kind"] == "source"
    assert result["data"]["requires_verification"] is True
    assert (workspace / path).read_text(encoding="utf-8") == content
    assert any(change.get("path") == str((workspace / path).resolve()) for change in store.data["changes"])


@pytest.mark.parametrize("path", ["game.exe", "plugin.dll", "bundle.zip", "image.png", "no_extension"])
def test_binary_and_unknown_extensions_are_rejected(source_runtime, path):
    workspace, _, runtime = source_runtime

    result = create(runtime, path, "not really binary")

    assert result["error"]["code"] == "unsupported_format"
    assert not (workspace / path).exists()


@pytest.mark.parametrize("path", ["../escape.py", ".env", ".env.local", ".genesis/secret.py"])
def test_source_create_respects_workspace_and_secret_boundaries(source_runtime, path):
    workspace, _, runtime = source_runtime

    result = create(runtime, path, "secret")

    assert result["error"]["code"] == "permission_denied"
    assert not (workspace / path).exists()


def test_existing_source_is_not_overwritten(source_runtime):
    workspace, _, runtime = source_runtime
    target = workspace / "main.py"
    target.write_text("original\n", encoding="utf-8")

    result = create(runtime, "main.py", "replacement\n")

    assert result["error"]["code"] == "target_exists"
    assert target.read_text(encoding="utf-8") == "original\n"


def test_malformed_html_is_created_but_not_marked_verified(source_runtime):
    _, _, runtime = source_runtime

    result = create(runtime, "broken.html", "<html><body><script>function x( {</script></body></html>")

    assert result["ok"]
    assert result["data"]["validation"]["passed"] is False
    assert result["data"]["validation"]["errors"]
