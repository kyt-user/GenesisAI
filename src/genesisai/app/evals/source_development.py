"""源码创建、续写和完成门禁的离线模拟测试。"""

from __future__ import annotations

import argparse
import json
import shutil
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


class ScriptedModel:
    model = "source-development-simulator"

    def __init__(self, *responses: Response):
        self.responses = list(responses)

    def chat(self, messages, tools=None, **kwargs):
        if not self.responses:
            raise AssertionError("源码开发仿真响应已耗尽")
        return self.responses.pop(0)

    def stream_chat(self, messages, tools=None, **kwargs):
        yield self.chat(messages, tools, **kwargs)


def tool(identifier: str, name: str, arguments: dict) -> Response:
    return Response(
        tool_calls=[ToolCall(identifier, name, json.dumps(arguments, ensure_ascii=False))],
        finish_reason="tool_calls",
    )


def load_dataset(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"cases", "forbidden"}:
        raise ValueError("源码开发仿真 manifest 必须且只能包含 cases 和 forbidden")
    identifiers = set()
    for case in data.get("cases", []):
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ValueError("源码开发仿真案例字段无效")
        if not isinstance(case["id"], str) or case["id"] in identifiers:
            raise ValueError("源码开发仿真案例 ID 无效或重复")
        if not isinstance(case["expectations"], list) or not case["expectations"]:
            raise ValueError("源码开发仿真案例缺少 expectations")
        identifiers.add(case["id"])
    if not identifiers or not isinstance(data.get("forbidden"), list):
        raise ValueError("源码开发仿真数据为空")
    return data


def case_environment(output: Path, identifier: str) -> tuple[Path, Store, ToolRuntime]:
    root = (output / identifier).resolve()
    if root.exists():
        if not root.is_relative_to(output.resolve()):
            raise ValueError("仿真清理目标越界")
        shutil.rmtree(root)
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    manager = AgentDocsManager(workspace, create=True)
    manager.begin_task(f"源码开发仿真：{identifier}")
    manager.set_plan(steps=["创建项目文件", "执行验证"], acceptance=["产物存在且验证状态真实"])
    store = Store(workspace / ".genesis", workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(workspace), deadline=time.time() + 300)
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], workspace, store.root, workspace_root=workspace),
        providers=[], confirm_writes=False, confirm_search=False, confirm_shell=False,
    )
    return workspace, store, runtime


def html_case(output: Path) -> dict:
    workspace, store, runtime = case_environment(output, "html_continuation")
    html = (
        "<!doctype html><html><body><label>猜数字<input id=\"guess\"></label>"
        "<button id=\"submit\">提交</button><button id=\"restart\">重新开始</button>"
        "<script>let answer=42;localStorage.setItem('best','1');"
        "document.querySelector('#submit').onclick=()=>answer;</script></body></html>\n"
    )
    result = Runner(ScriptedModel(
        tool("html_create", "editor", {"operation": "create", "path": "index.html", "content": html, "source_refs": []}),
        Response(content="页面已写入并通过静态验收。"),
    ), runtime).start("按你的默认来")
    task = AgentDocsManager(workspace).active_task()
    checks = {
        "runner_completed": result["status"] == "completed",
        "profile_inherited": store.data["run_runtime"]["profile"] == "local_files",
        "html_written": (workspace / "index.html").read_text(encoding="utf-8") == html,
        "verified": bool(task and task["status"] == "verified"),
    }
    return {"id": "html_continuation", "status": "passed" if all(checks.values()) else "failed", "checks": checks}


def python_case(output: Path) -> dict:
    workspace, store, runtime = case_environment(output, "python_creation")
    result = Runner(ScriptedModel(
        tool("py_create", "editor", {"operation": "create", "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n", "source_refs": []}),
        tool("test_create", "editor", {"operation": "create", "path": "test_calculator.py", "content": "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", "source_refs": []}),
        Response(content="源码和测试已写入。"),
        Response(content="Python 测试通过。"),
    ), runtime).start("开始")
    checks = {
        "runner_completed": result["status"] == "completed",
        "source_written": (workspace / "calculator.py").is_file(),
        "test_written": (workspace / "test_calculator.py").is_file(),
        "verification_succeeded": store.data["run_runtime"].get("verification_succeeded") is True,
    }
    return {"id": "python_creation", "status": "passed" if all(checks.values()) else "failed", "checks": checks}


def java_case(output: Path) -> dict:
    workspace, store, runtime = case_environment(output, "java_limited_verification")
    result = Runner(ScriptedModel(
        tool("java_create", "editor", {"operation": "create", "path": "Main.java", "content": "public class Main { public static void main(String[] args) {} }\n", "source_refs": []}),
        Response(content="Java 源码已写入。"),
        Response(content="当前没有自动 Java 验证入口。"),
        Response(content="无法完成验证。"),
        Response(content="保留为待验证。"),
    ), runtime, max_rounds=12).start("开始")
    checks = {
        "source_written": (workspace / "Main.java").is_file(),
        "not_completed": result["status"] != "completed",
        "verification_not_faked": store.data["run_runtime"].get("verification_succeeded") is not True,
    }
    return {"id": "java_limited_verification", "status": "passed" if all(checks.values()) else "failed", "checks": checks}


def binary_case(output: Path) -> dict:
    workspace, store, runtime = case_environment(output, "unsupported_binary")
    result = Runner(ScriptedModel(
        tool("binary_create", "editor", {"operation": "create", "path": "game.exe", "content": "binary", "source_refs": []}),
        Response(content="无法写入。"),
        Response(content="任务未产生文件。"),
    ), runtime).start("开始")
    error = ((store.data["calls"].get("binary_create") or {}).get("result") or {}).get("error") or {}
    checks = {
        "file_absent": not (workspace / "game.exe").exists(),
        "structured_error": error.get("code") == "unsupported_format",
        "not_completed": result["status"] != "completed",
    }
    return {"id": "unsupported_binary", "status": "passed" if all(checks.values()) else "failed", "checks": checks}


def run_all(dataset: Path, output: Path) -> dict:
    definition = load_dataset(dataset)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    handlers = {
        "html_continuation": html_case,
        "python_creation": python_case,
        "java_limited_verification": java_case,
        "unsupported_binary": binary_case,
    }
    results = []
    for case in definition["cases"]:
        detail = handlers[case["id"]](output)
        detail.update(name=case["name"], expectations=case["expectations"])
        results.append(detail)
    report = {
        "suite": "source_development_v1",
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "forbidden": definition["forbidden"],
        "results": results,
    }
    atomic_json(output / "report-source-development.json", report)
    return report


def main(argv=None):
    project = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description="GenesisAI 源码开发离线仿真验收")
    parser.add_argument("--dataset", type=Path, default=project / "tests" / "source_development_dataset" / "manifest.yaml")
    parser.add_argument("--output", type=Path, default=project / ".genesis" / "source-development-simulation")
    args = parser.parse_args(argv)
    report = run_all(args.dataset, args.output)
    print(json.dumps({"status": report["status"], "cases": [{"id": item["id"], "status": item["status"]} for item in report["results"]]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
