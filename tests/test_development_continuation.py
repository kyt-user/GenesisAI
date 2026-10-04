import time

from genesisai.agent.runner import Runner
from genesisai.core.project_docs import AgentDocsManager
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from genesisai.core.state.store import Store
from test_acceptance import FakeModel


def development_env(tmp_path, *, planned=True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = AgentDocsManager(workspace, create=True)
    task = manager.begin_task("构建一个 HTML 猜数字小游戏")
    if planned:
        manager.set_plan(steps=["创建 index.html"], acceptance=["页面可离线运行"])
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
    return workspace, manager, task, store, runtime


def test_accept_default_continues_active_development_task(tmp_path):
    _, _, task, store, runtime = development_env(tmp_path)
    model = FakeModel(Response(content="我会开始。"), Response(content="仍未写入。"))

    result = Runner(model, runtime).start("按你的默认来")

    state = store.data["run_runtime"]
    assert result["status"] == "partial"
    assert state["profile"] == "local_files"
    assert "coding" in state["protocols"]
    assert state["objective"] == "构建一个 HTML 猜数字小游戏"
    assert state["project_task_id"] == task["id"]
    assert state["pending_intent"]["instruction"] == "按你的默认来"
    assert state["pending_intent"]["default_plan_confirmed"] is True
    assert state["pending_intent"]["next_action"] == "implement_next_step"
    assert state["pending_intent"]["awaiting_confirmation"] is False
    assert state["execution_requested"] is True


def test_start_without_active_task_does_not_invent_development_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentDocsManager(workspace, create=True)
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

    result = Runner(FakeModel(Response(content="你想开始什么项目？")), runtime).start("开始")

    assert result["status"] == "completed"
    assert store.data["run_runtime"]["profile"] == "direct_answer"
    assert "coding" not in store.data["run_runtime"]["protocols"]
    assert "pending_intent" not in store.data["run_runtime"]


def test_unrelated_question_does_not_continue_active_task(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)

    Runner(FakeModel(Response(content="无法提供实时天气。")), runtime).start("今天天气如何")

    state = store.data["run_runtime"]
    assert state["profile"] == "web_quick"
    assert "pending_intent" not in state
    assert state["execution_requested"] is False
