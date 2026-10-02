import time

from genesisai.agent.runner import Runner
from genesisai.runtime.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from genesisai.state.store import Store, sha
from test_acceptance import FakeModel


def test_local_file_source_reference_is_valid_without_web_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "plan.md"
    note.write_text("local plan", encoding="utf-8")
    store = Store(workspace / ".genesis", workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(workspace), deadline=time.time() + 300)
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        confirm_writes=False,
        confirm_search=False,
        confirm_shell=False,
        providers=[],
    )
    ref = store.source(kind="file", path=str(note), sha256=sha(note), title=note.name)

    result = Runner(FakeModel(Response(content=f"计划见 [{ref}]。")), runtime).start("概括本地文件")

    assert result["status"] == "completed"
    assert result["answer"] == f"计划见 [{ref}]。"
