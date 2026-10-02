"""First-version acceptance for workspace onboarding and Python project development."""

from pathlib import Path

import pytest

from genesisai.app.cli import select_workspace
from genesisai.capabilities.shell.python_support import automatic_test_command, inspect_python_project
from genesisai.evals.python_development import load_dataset, run_all
from genesisai.project_docs import AgentDocsError, AgentDocsManager
from genesisai.state.store import workspace_state_root


PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "agent_tests_workspace"
DATASET = Path(__file__).parent / "python_development_dataset" / "manifest.yaml"


class _Console:
    def __init__(self, answer):
        self.answer = answer

    def input(self, *_args, **_kwargs):
        return self.answer


class _View:
    def __init__(self):
        self.values = None

    def render_workspace_selection(self, current, suggested):
        self.values = (current, suggested)


def test_interactive_workspace_is_selected_before_session(tmp_path, monkeypatch):
    workspace = tmp_path / "agent_tests_workspace"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    view = _View()

    selected = select_workspace(None, console=_Console(str(workspace)), view=view, interactive=True)

    assert selected == workspace.resolve()
    assert view.values[0] == str(tmp_path.resolve())


def test_interactive_workspace_must_already_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="已经存在"):
        select_workspace(None, console=_Console("missing"), view=_View(), interactive=True)


def test_interactive_enter_explicitly_selects_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    selected = select_workspace(None, console=_Console(""), view=_View(), interactive=True)

    assert selected == tmp_path.resolve()


def test_noninteractive_start_requires_explicit_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="--workspace"):
        select_workspace(None, console=_Console(""), view=_View(), interactive=False)


def test_runtime_state_is_contained_by_selected_workspace(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    assert workspace_state_root(workspace) == workspace / ".genesis"


def test_cli_rejects_input_and_output_outside_selected_workspace(tmp_path):
    import genesisai.app.cli as cli

    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert cli.main(["--workspace", str(workspace), "--input", str(outside)]) == 2
    assert cli.main(["--workspace", str(workspace), "--output", str(outside)]) == 2
    assert cli.main(["--workspace", str(workspace), "--output", str(workspace / ".genesis" / "out")]) == 2


def test_noninteractive_cli_can_initialize_agent_docs(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from test_acceptance import FakeModel

    monkeypatch.setattr(cli, "build_model", lambda _path: (FakeModel(), False))
    commands = iter(["/project", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    workspace = tmp_path / "agent_tests_workspace"

    assert cli.main(["--workspace", str(workspace), "--yes-agent-docs"]) == 0
    assert AgentDocsManager.exists(workspace)


def test_selected_workspace_reaches_prompt_and_default_file_output(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from genesisai.shared.messages import Response
    from genesisai.runtime.tool_runtime import ToolRuntime
    from genesisai.shared.security import Access
    from genesisai.state.store import Store
    from test_acceptance import FakeModel, call

    workspace = tmp_path / "agent_tests_workspace"
    workspace.mkdir()
    model = FakeModel(Response(content="done"))
    monkeypatch.setattr(cli, "build_model", lambda _path: (model, False))

    code = cli.main([
        "--workspace", str(workspace),
        "--no-agent-docs", "--prompt", "你知道当前工作区吗",
    ])

    assert code == 0
    system_prompt = model.contexts[0][0].content
    assert str(workspace.resolve()) in system_prompt
    assert "不要调用 Shell、pwd 或其他工具重新探测" in system_prompt

    store = Store(workspace / ".genesis", workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(workspace))
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        providers=[], confirm_writes=False, confirm_search=False,
    )
    runtime.load_tools(["file_create"])
    result = runtime.execute(call("file_create", {
        "path": "hello.txt", "content": "hello", "source_refs": [],
    }, "create_hello"))

    assert result["ok"] is True
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert not (workspace / ".genesis" / "hello.txt").exists()


def test_agent_docs_ownership_plan_resume_and_conflict(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    manager = AgentDocsManager(workspace, create=True)
    task = manager.begin_task("实现小型 Python 项目", run_id="run_one")
    planned = manager.set_plan(steps=["读取项目", "实现功能"], acceptance=["测试通过"])

    restarted = AgentDocsManager(workspace)
    resumed = restarted.begin_task("继续任务", run_id="run_two")

    assert planned["plan_ready"] is True
    assert resumed["id"] == task["id"]
    assert "run_two" in resumed["run_ids"]
    plan = workspace / "agent_docs" / "tasks" / task["id"] / "plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")
    with pytest.raises(AgentDocsError, match="外部修改"):
        restarted.set_plan(steps=["覆盖"], acceptance=["不允许"])


def test_unowned_agent_docs_is_not_taken_over(tmp_path):
    workspace = tmp_path / "work"
    (workspace / "agent_docs").mkdir(parents=True)
    (workspace / "agent_docs" / "user.md").write_text("mine", encoding="utf-8")

    with pytest.raises(AgentDocsError, match="停止接管"):
        AgentDocsManager(workspace, create=True)


def test_python_project_detection_supports_pytest_unittest_and_compileall(tmp_path):
    pytest_project = tmp_path / "pytest_project"
    (pytest_project / "tests").mkdir(parents=True)
    (pytest_project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (pytest_project / "tests" / "test_x.py").write_text("def test_x(): assert True\n", encoding="utf-8")
    pytest_command, pytest_details = automatic_test_command(pytest_project)

    unittest_project = tmp_path / "unittest_project"
    (unittest_project / "tests").mkdir(parents=True)
    (unittest_project / "tests" / "test_x.py").write_text("import unittest\n", encoding="utf-8")
    unittest_command, unittest_details = automatic_test_command(unittest_project)

    source_project = tmp_path / "source_project"
    source_project.mkdir()
    (source_project / "module.py").write_text("value = 1\n", encoding="utf-8")
    compile_command, compile_details = automatic_test_command(source_project)

    assert pytest_details["test_framework"] == "pytest" and pytest_command[-3:] == ["-m", "pytest", "-q"]
    assert unittest_details["test_framework"] == "unittest" and "unittest" in unittest_command
    assert compile_details["test_framework"] == "compileall" and "compileall" in compile_command
    assert inspect_python_project(pytest_project)["test_file_count"] == 1


def test_python_development_dataset_is_strict():
    data = load_dataset(DATASET)
    assert [item["id"] for item in data["cases"]] == [
        "development_loop", "resume_and_drift", "failed_acceptance_gate",
    ]


def test_python_development_simulation_passes(tmp_path):
    report = run_all(DATASET, FIXTURE, tmp_path / "simulation")

    assert report["status"] == "passed"
    assert all(item["status"] == "passed" for item in report["results"])
    assert (tmp_path / "simulation" / "report-python-development.json").is_file()
