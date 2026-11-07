"""First-version acceptance for workspace onboarding and Python project development."""

from pathlib import Path

import pytest

from genesisai.app.cli import select_workspace
from genesisai.core.extensions.tools.shell.python_support import automatic_test_command, inspect_python_project
from genesisai.app.evals.python_development import load_dataset, run_all
from genesisai.core.memory import TaskService
from genesisai.core.persistence.session import Session
from genesisai.shared.paths import application_home, default_workspace



PROJECT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT / "tests" / "fixtures" / "python_project"
DATASET = Path(__file__).parent / "python_development_dataset" / "manifest.yaml"


def test_default_workspace_is_independent_of_launch_directory(tmp_path):
    assert default_workspace() == tmp_path / "home" / ".genesisai" / "workspace"
    assert select_workspace(None) == default_workspace()
    assert default_workspace().is_dir()


def test_explicit_workspace_is_created_and_overrides_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selected = select_workspace(Path("custom project"))
    assert selected == tmp_path / "custom project"
    assert selected.is_dir()
    assert not default_workspace().exists()


def test_workspace_cannot_be_a_file(tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(ValueError, match="目录"):
        select_workspace(target)


def test_current_directory_does_not_change_default(tmp_path, monkeypatch):
    first = select_workspace(None)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    assert select_workspace(None) == first
    assert not (other / ".genesis").exists()


def test_runtime_history_is_outside_default_workspace():
    assert application_home() == Path.home() / ".genesisai"
    with pytest.raises(ValueError, match="历史目录"):
        select_workspace(application_home() / "nested")


def test_cli_rejects_input_and_output_outside_selected_workspace(tmp_path):
    import genesisai.app.cli as cli

    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert cli.main(["--workspace", str(workspace), "--input", str(outside)]) == 2
    assert cli.main(["--workspace", str(workspace), "--output", str(outside)]) == 2
    assert cli.main(["--workspace", str(tmp_path), "--output", str(application_home() / "state")]) == 2


def test_noninteractive_cli_maintains_memory_outside_project(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from test_acceptance import FakeModel

    monkeypatch.setattr(cli, "build_model", lambda _path: (FakeModel(), False))
    commands = iter(["/project", "/exit"])
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(commands))
    workspace = tmp_path / "agent_tests_workspace"

    assert cli.main(["--workspace", str(workspace)]) == 0
    assert not (workspace / "agent_docs").exists()
    assert (application_home() / "state" / "genesis.sqlite3").exists()


def test_selected_workspace_reaches_prompt_and_default_file_output(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    from genesisai.shared.messages import Response
    from genesisai.core.tools.tool_runtime import ToolRuntime
    from genesisai.shared.security import Access
    from genesisai.core.persistence.session import Session
    from test_acceptance import FakeModel, call

    workspace = tmp_path / "agent_tests_workspace"
    workspace.mkdir()
    model = FakeModel(Response(content="done"))
    monkeypatch.setattr(cli, "build_model", lambda _path: (model, False))

    code = cli.main([
        "--workspace", str(workspace),
        "--prompt", "你知道当前工作区吗",
    ])

    assert code == 0
    system_prompt = "\n".join(str(m.content) for m in model.contexts[0])
    assert str(workspace.resolve()) in system_prompt
    assert "不要用工具重新探测工作区" in system_prompt

    store = Session(workspace / ".genesis", workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        providers=[], permission_mode="auto",
    )
    result = runtime.execute(call("editor", {
        "operation": "create", "path": "hello.txt", "content": "hello", "source_refs": [],
    }, "create_hello"))

    assert result["ok"] is True
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert not (workspace / ".genesis" / "hello.txt").exists()


def test_application_task_plan_resumes_without_project_files(tmp_path):
    workspace=tmp_path/'work'
    workspace.mkdir()
    session=Session(tmp_path/'app',workspace=workspace)
    manager=TaskService(session)
    task=manager.begin_task('实现项目',run_id='run_one')
    manager.update_plan(expected_revision=0, upsert=[
        {'id': 'implement', 'kind': 'step', 'text': '实现功能'},
        {'id': 'tests', 'kind': 'acceptance', 'text': '测试通过', 'requires_verification': True},
    ])
    restarted=TaskService(Session(session.root,session.id))
    resumed=restarted.begin_task('继续',run_id='run_two')
    assert resumed['id']==task['id'] and resumed['plan_ready']
    assert resumed['run_ids']==['run_one','run_two']
    assert list(workspace.iterdir())==[]


def test_user_owned_files_are_untouched_by_memory(tmp_path):
    workspace=tmp_path/'work'
    (workspace/'agent_docs').mkdir(parents=True)
    target=workspace/'agent_docs'/'user.md'
    target.write_text('mine',encoding='utf-8')
    manager=TaskService(Session(tmp_path/'app',workspace=workspace))
    manager.begin_task('任务')
    assert target.read_text(encoding='utf-8')=='mine'
    assert list((workspace/'agent_docs').iterdir())==[target]


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
