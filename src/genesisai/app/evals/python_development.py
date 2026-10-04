"""Python 小项目开发循环的离线验收模拟。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

import yaml

from genesisai.agent.runner import Runner
from genesisai.core.project_docs import AgentDocsManager
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.messages import Response, ToolCall
from genesisai.shared.security import Access
from genesisai.core.state.store import Store, atomic_json


CASE_FIELDS = frozenset({"id", "name", "expectations"})


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def load_dataset(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"cases", "forbidden"}:
        raise ValueError("Python 仿真 manifest 必须且只能包含 cases 和 forbidden")
    if not isinstance(data["cases"], list) or not isinstance(data["forbidden"], list):
        raise ValueError("Python 仿真 cases/forbidden 必须是数组")
    identifiers = set()
    for case in data["cases"]:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ValueError("Python 仿真案例字段无效")
        if not isinstance(case["id"], str) or case["id"] in identifiers:
            raise ValueError("Python 仿真案例 ID 无效或重复")
        if not isinstance(case["expectations"], list) or not case["expectations"]:
            raise ValueError("Python 仿真案例缺少 expectations")
        identifiers.add(case["id"])
    return data


class ScriptedModel:
    """A deterministic model that still exercises the real Runner loop."""

    model = "python-development-simulator"

    def __init__(self, responses: list[Response]):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if not self.responses:
            raise AssertionError("仿真模型响应已耗尽")
        return self.responses.pop(0)

    def stream_chat(self, messages, tools=None):
        yield self.chat(messages, tools)


def _tool(identifier: str, name: str, arguments: dict) -> Response:
    return Response(
        tool_calls=[ToolCall(id=identifier, name=name, arguments=json.dumps(arguments, ensure_ascii=False))],
        finish_reason="tool_calls",
    )


def _runtime(workspace: Path, case_root: Path) -> tuple[Store, ToolRuntime]:
    state = case_root / "state"
    output = case_root / "outputs"
    output.mkdir(parents=True, exist_ok=True)
    store = Store(state, workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(output), run_id="run_python_simulation")
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], output, state, workspace_root=workspace),
        providers=[], confirm_writes=False, confirm_search=False, confirm_shell=False,
    )
    return store, runtime


def _prepare_case(fixture: Path, output: Path, identifier: str) -> tuple[Path, Path, dict]:
    case_root = output / identifier
    if case_root.exists():
        resolved = case_root.resolve()
        if not resolved.is_relative_to(output.resolve()):
            raise ValueError("仿真清理目标越界")
        shutil.rmtree(case_root)
    workspace = case_root / "workspace"
    shutil.copytree(fixture, workspace)
    return case_root, workspace, inventory(fixture)


def _run_development_loop(fixture: Path, output: Path) -> dict:
    case_root, workspace, baseline = _prepare_case(fixture, output, "development_loop")
    # 目标项目拥有独立环境。它继承离线测试工具包，
    # 因此模拟不需要网络安装，而 python_project/test_run 仍需发现并执行本地 Python。
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(workspace / ".venv")],
        cwd=workspace, check=True, capture_output=True,
    )
    project_python = workspace / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    project_site = subprocess.run(
        [str(project_python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        cwd=workspace, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    # 验收工具包是离线的。通过 .pth 文件将已安装的工具包
    # 暴露给这个一次性的项目解释器。
    (Path(project_site) / "genesisai_simulation_host.pth").write_text(
        str(Path(sysconfig.get_paths()["purelib"]).resolve()) + "\n", encoding="utf-8",
    )
    AgentDocsManager(workspace, create=True)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=GenesisAI", "-c", "user.email=simulation@example.invalid", "commit", "-m", "fixture"],
        cwd=workspace, check=True, capture_output=True,
    )
    target = workspace / "src" / "taskboard" / "service.py"
    expected_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    original_test = (workspace / "tests" / "test_service.py").read_bytes()
    patch = (
        "@@ -4,3 +4,10 @@\n"
        " def normalize_tags(values: list[str]) -> list[str]:\n"
        "     \"\"\"Return normalized, unique, non-empty tags in their original order.\"\"\"\n"
        "-    return [value.lower() for value in values]\n"
        "+    normalized = []\n"
        "+    seen = set()\n"
        "+    for value in values:\n"
        "+        tag = value.strip().casefold()\n"
        "+        if tag and tag not in seen:\n"
        "+            seen.add(tag)\n"
        "+            normalized.append(tag)\n"
        "+    return normalized"
    )
    responses = [
        _tool("plan", "plan", {
            "steps": ["检查 Python 项目与失败测试", "修复标签规范化逻辑", "检查 Git 差异并运行完整测试"],
            "acceptance": ["原有测试文件保持不变", "pytest 全部通过", "任务状态为 verified"],
            "non_goals": ["不修改公共 API", "不执行 Git 写操作"],
        }),
        _tool("inspect", "run_commands", {"operation": "inspect", "cwd": "."}),
        _tool("reproduce", "run_commands", {"operation": "run", "cwd": ".", "timeout_seconds": 60}),
        _tool("read", "read_files", {"path": "src/taskboard/service.py"}),
        _tool("patch", "editor", {
            "operation": "patch", "path": "src/taskboard/service.py", "patch": patch, "expected_sha256": expected_sha,
        }),
        _tool("diff", "run_commands", {"operation": "run", "command": ["git", "diff"], "cwd": "."}),
        Response(content="实现已完成，准备交付。"),
        Response(content="已完成标签规范化修复；pytest 已通过，测试文件未修改。"),
    ]
    store, runtime = _runtime(workspace, case_root)
    result = Runner(ScriptedModel(responses), runtime, max_rounds=20, seconds=120).start(
        "修复这个 Python 小项目的标签规范化缺陷，先策划和编写计划，再执行并验收；不得修改测试。"
    )
    external = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=workspace,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    manager = AgentDocsManager(workspace)
    task = manager.active_task()
    called = [item.get("call", {}).get("name") for item in store.data["calls"].values()]
    inspection = ((store.data["calls"].get("inspect") or {}).get("result") or {}).get("data") or {}
    run_calls = [
        ((item.get("result") or {}).get("data") or {})
        for item in store.data["calls"].values()
        if (item.get("call") or {}).get("name") == "run_commands"
    ]
    test_commands = [data.get("command", []) for data in run_calls if "summary" in data]
    diff_checked = any((data.get("command") or [""])[:1] == ["git"] for data in run_calls)
    checks = {
        "runner_completed": result["status"] == "completed",
        "plan_ready": bool(task and task["plan_ready"]),
        "python_inspected": bool(inspection),
        "project_venv_detected": inspection.get("interpreter_source") == ".venv",
        "project_venv_executed": bool(test_commands) and all(
            command and Path(command[0]).resolve().is_relative_to((workspace / ".venv").resolve())
            for command in test_commands
        ),
        "failure_reproduced": len(test_commands) >= 2,
        "diff_checked": diff_checked,
        "automatic_verification_survived_diff": bool(task and task["status"] == "verified"),
        "external_tests_passed": external.returncode == 0,
        "tests_unchanged": (workspace / "tests" / "test_service.py").read_bytes() == original_test,
        "fixture_unchanged": inventory(fixture) == baseline,
    }
    return {
        "id": "development_loop", "status": "passed" if all(checks.values()) else "failed",
        "checks": checks, "runner_status": result["status"], "called_tools": called,
        "task": task, "pytest": (external.stdout + external.stderr)[-2000:],
    }


def _run_resume_and_drift(fixture: Path, output: Path) -> dict:
    _, workspace, baseline = _prepare_case(fixture, output, "resume_and_drift")
    first = AgentDocsManager(workspace, create=True)
    task = first.begin_task("继续实现 Python 项目", run_id="run_before_restart")
    first.set_plan(steps=["检查实现", "运行测试"], acceptance=["测试通过"])
    target = workspace / "src" / "taskboard" / "service.py"
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    first.record_tool("read_files", {"ok": True, "data": {"files": [{"path": "src/taskboard/service.py", "sha256": digest}]}}, {"path": "src/taskboard/service.py"})
    restarted = AgentDocsManager(workspace)
    before = restarted.begin_task("继续实现 Python 项目", run_id="run_after_restart")
    restarted.record_tool("run_commands", {"ok": True, "data": {"passed": True, "command": ["python", "-m", "pytest", "-q"], "exit_code": 0, "summary": {"passed": 2}}})
    restarted.finish_run("completed", "已完成首次验收")
    with target.open("a", encoding="utf-8") as stream:
        stream.write("\n# external drift\n")
    drifted = restarted.reconcile()
    current = restarted.active_task()
    checks = {
        "same_task_resumed": before["id"] == task["id"],
        "new_run_recorded": "run_after_restart" in current["run_ids"],
        "drift_detected": "src/taskboard/service.py" in drifted,
        "verification_invalidated": current["status"] == "needs_verification" and not current["verification_passed"],
        "fixture_unchanged": inventory(fixture) == baseline,
    }
    return {"id": "resume_and_drift", "status": "passed" if all(checks.values()) else "failed", "checks": checks, "task": current}


def _run_failed_gate(fixture: Path, output: Path) -> dict:
    case_root, workspace, baseline = _prepare_case(fixture, output, "failed_acceptance_gate")
    AgentDocsManager(workspace, create=True)
    target = workspace / "src" / "taskboard" / "service.py"
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    responses = [
        _tool("failed_plan", "plan", {"steps": ["尝试修复产品代码"], "acceptance": ["pytest 通过"]}),
        _tool("failed_read", "read_files", {"path": "src/taskboard/service.py"}),
        _tool("failed_patch", "editor", {
            "operation": "patch",
            "path": "src/taskboard/service.py",
            "patch": '@@ -1,1 +1,1 @@\n-"""Task creation domain logic."""\n+"""Task creation domain logic used in tests."""',
            "expected_sha256": digest,
        }),
        Response(content="修改完成。"),
        Response(content="测试仍未通过。"),
        Response(content="尚未找到修复。"),
        Response(content="当前改动未能通过验收。"),
    ]
    store, runtime = _runtime(workspace, case_root)
    result = Runner(ScriptedModel(responses), runtime, max_rounds=16, seconds=120).start(
        "修复项目 API_KEY=should-not-leak；测试不通过时不得宣称完成。"
    )
    manager = AgentDocsManager(workspace)
    task = manager.active_task()
    contents = "\n".join(path.read_text(encoding="utf-8") for path in manager.root.rglob("*.md"))
    checks = {
        "runner_not_completed": result["status"] == "partial",
        "not_verified": task["status"] == "needs_verification",
        "verification_retried_with_bound": sum(
            1 for item in store.data["calls"].values()
            if (item.get("call") or {}).get("name") == "run_commands"
            and "summary" in (((item.get("result") or {}).get("data")) or {})
        ) == 3,
        "failed_result_recorded": "结果：失败" in contents and "退出码：`1`" in contents,
        "secret_redacted": "should-not-leak" not in contents and "[REDACTED]" in contents,
        "fixture_unchanged": inventory(fixture) == baseline,
    }
    return {
        "id": "failed_acceptance_gate", "status": "passed" if all(checks.values()) else "failed",
        "checks": checks, "runner_status": result["status"], "task": task,
    }


def run_all(dataset: Path, fixture: Path, output: Path) -> dict:
    definition = load_dataset(dataset)
    fixture = Path(fixture).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    handlers = {
        "development_loop": _run_development_loop,
        "resume_and_drift": _run_resume_and_drift,
        "failed_acceptance_gate": _run_failed_gate,
    }
    results = []
    for case in definition["cases"]:
        detail = handlers[case["id"]](fixture, output)
        detail["name"] = case["name"]
        detail["expectations"] = case["expectations"]
        results.append(detail)
    report = {
        "suite": "python_development_v1", "platform": platform.platform(), "python": platform.python_version(),
        "started_at": started, "finished_at": time.time(), "forbidden": definition["forbidden"],
        "status": "passed" if results and all(item["status"] == "passed" for item in results) else "failed",
        "results": results,
    }
    atomic_json(output / "report-python-development.json", report)
    return report


def main(argv=None):
    project = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="GenesisAI Python 小项目开发仿真验收")
    parser.add_argument("--dataset", type=Path, default=project / "tests" / "python_development_dataset" / "manifest.yaml")
    parser.add_argument("--workspace", type=Path, default=project / "agent_tests_workspace")
    parser.add_argument("--output", type=Path, default=Path.cwd() / ".genesis" / "python-development-simulation")
    args = parser.parse_args(argv)
    report = run_all(args.dataset, args.workspace, args.output)
    print(json.dumps({"status": report["status"], "cases": [{"id": item["id"], "status": item["status"]} for item in report["results"]]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
