"""P2-P6 冻结能力的确定性阶段验收。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from genesisai.shared.changes import undo_changes
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.shared.filetypes import detect_file_type
from genesisai.app.maintenance import clean_runtime, doctor
from genesisai.core.prompt.composer import PromptComposer, PromptFormatError
from genesisai.shared.security import Access
from genesisai.core.extensions.skills import SkillError, SkillRegistry
from genesisai.agent.state_machine import RunStateMachine, StateTransitionError
from genesisai.core.state.store import Store, sha
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.tools.base import ToolSpec, object_schema
from genesisai.core.tools.tool_runtime import ToolRuntime


def runtime_for(tmp_path, *, active=()):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    state = tmp_path / "state"; output = tmp_path / "output"
    store = Store(state, workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(output), run_id="run_test")
    store.save()
    runtime = ToolRuntime(store, Access([workspace], output, state, workspace_root=workspace), providers=[], confirm_writes=False, confirm_search=False, confirm_shell=False)
    return workspace, output, store, runtime


def execute(runtime, name, args, ident=None):
    call = {"id": ident or "call_" + name, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
    return runtime.execute(call)


def test_p2_prompt_state_file_patch_shell_and_git(tmp_path):
    raw = """name: x\ncategory: protocol\ndescription: x\ncontent:\n  objective: x\n  instructions: []\n  constraints: []\n  completion: []\n"""
    assert PromptComposer.parse(raw)["name"] == "x"
    with pytest.raises(PromptFormatError): PromptComposer.parse(raw + "version: 1\n")
    workspace, _, store, runtime = runtime_for(tmp_path)
    target = workspace / "calc.py"; target.write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    read = execute(runtime, "read_files", {"path": "calc.py", "line_start": 1, "line_end": 2})
    entry = read["data"]["files"][0]
    assert read["ok"] and entry["newline"] == "CRLF" and "2:" in entry["text"]
    digest = entry["sha256"]
    patch = "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b"
    changed = execute(runtime, "editor", {"operation": "patch", "path": "calc.py", "patch": patch, "expected_sha256": digest})
    assert changed["ok"] and target.read_bytes().endswith(b"a + b\r\n") and store.data["changes"]
    conflict = execute(runtime, "editor", {"operation": "patch", "path": "calc.py", "patch": patch, "expected_sha256": digest}, "conflict")
    assert not conflict["ok"] and conflict["error"]["code"] == "patch_conflict"
    shell = execute(runtime, "run_commands", {"operation": "run", "command": [sys.executable, "-c", "print('ok')"], "cwd": ".", "timeout_seconds": 10})
    assert shell["ok"] and shell["data"]["exit_code"] == 0 and "ok" in shell["data"]["stdout"]
    blocked = execute(runtime, "run_commands", {"operation": "run", "command": ["git", "commit", "-m", "x"], "cwd": "."}, "blocked_git")
    assert not blocked["ok"] and blocked["error"]["code"] == "git_write_forbidden"
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    status = execute(runtime, "run_commands", {"operation": "run", "command": ["git", "rev-parse", "--show-toplevel"], "cwd": "."}, "git_toplevel")
    assert status["ok"] and "workspace" in status["data"]["stdout"]
    machine = RunStateMachine(store); store.data["run_runtime"]["lifecycle"] = "ready"
    machine.transition("prepare", "test"); machine.transition("execute", "test")
    with pytest.raises(StateTransitionError): machine.transition("ready", "illegal")


def test_p2_single_file_pytest_project_is_detected(tmp_path):
    workspace, _, _, runtime = runtime_for(tmp_path)
    (workspace / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = execute(runtime, "run_commands", {"operation": "run"})
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0


def test_p4_catalog_includes_patch_tool(tmp_path):
    _, _, _, runtime = runtime_for(tmp_path)
    enabled = {item["name"] for item in runtime.inventory()["enabled"]}
    assert "editor" in enabled


def test_p5_generated_output_can_be_read_by_relative_name(tmp_path):
    _, output, _, runtime = runtime_for(tmp_path)
    output.mkdir(parents=True, exist_ok=True)
    target = output / "report.txt"
    target.write_text("verified", encoding="utf-8")
    assert runtime.access.read("report.txt") == target.resolve()
    assert runtime.access.read("output/report.txt") == target.resolve()


def test_p5_optional_dependency_status_is_visible():
    spec = ToolSpec(parameters=object_schema([]), permission="read", dependencies=("genesisai_dependency_that_does_not_exist",))
    assert "genesisai[office]" in spec.unavailable_reason()


def test_p4_skill_priority_strict_schema_and_core_tools(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    root = workspace / ".genesis" / "skills" / "bug_fix"; root.mkdir(parents=True)
    (root / "skill.yaml").write_text("name: bug_fix\ndescription: project override\nentry: SKILL.md\ntools: [read_files]\n", encoding="utf-8")
    (root / "SKILL.md").write_text("# Project flow\n", encoding="utf-8")
    registry = ToolRegistry(); skills = SkillRegistry(workspace, registry)
    item = skills.describe("bug_fix", include_content=True)
    assert item["source"] == "project" and "builtin" in item["overridden"] and "Project flow" in item["content"]
    assert skills.load(["bug_fix"]) == ["bug_fix"]
    with pytest.raises(SkillError): skills.load(["bug_fix", "code_review", "web_research"])


def test_p5_docx_xlsx_pdf_and_optional_pptx(tmp_path):
    workspace, output, store, runtime = runtime_for(tmp_path, active=("docx_create", "docx_read", "xlsx_create", "xlsx_read", "pdf_create", "pdf_read"))
    doc = execute(runtime, "docx_create", {"path": "sample.docx", "title": "Report", "paragraphs": ["Hello"], "table": [["A", "B"]]})
    assert doc["ok"] and detect_file_type(output / "sample.docx") == "docx"
    read_doc = execute(runtime, "docx_read", {"path": str(output / "sample.docx")}); assert read_doc["ok"] and any(p["text"] == "Hello" for p in read_doc["data"]["paragraphs"])
    book = execute(runtime, "xlsx_create", {"path": "sample.xlsx", "sheets": {"Data": [[1, 2], ["=A1+B1", None]]}})
    assert book["ok"] and detect_file_type(output / "sample.xlsx") == "xlsx"
    read_book = execute(runtime, "xlsx_read", {"path": str(output / "sample.xlsx")}); assert read_book["ok"] and read_book["data"]["formulas_not_recalculated"]
    pdf = execute(runtime, "pdf_create", {"path": "sample.pdf", "lines": ["GenesisAI report"]}); assert pdf["ok"] and detect_file_type(output / "sample.pdf") == "pdf"
    assert execute(runtime, "pdf_read", {"path": str(output / "sample.pdf")})["ok"]
    assert all(item.get("verified") for item in store.data["artifacts"].values())


def test_p6_compact_undo_doctor_clean(tmp_path):
    workspace, output, store, runtime = runtime_for(tmp_path)
    for i in range(4):
        store.data["messages"].extend([{"role": "user", "content": f"goal {i}"}, {"role": "assistant", "content": f"answer {i}"}])
    store.save()
    summary = ContextBudgeter(store, PromptComposer(), max_chars=2000).compact(force=True)
    assert summary["source_hash"] and summary["user_goals"]
    created = execute(runtime, "editor", {"operation": "create", "path": "new.txt", "content": "hello", "source_refs": [], "scope": "workspace"})
    assert created["ok"] and (workspace / "new.txt").is_file()
    preview = undo_changes(store, workspace); assert preview and not preview[0]["conflict"]
    undo_changes(store, workspace, apply=True); assert not (workspace / "new.txt").exists()
    report = doctor(workspace, store.root); assert any(item["name"] == "workspace" and item["ok"] for item in report["checks"])
    old = store.root / "command_logs" / "old.log"; old.parent.mkdir(); old.write_text("x"); old.touch()
    cleaned = clean_runtime(store.root, store.id, execute=False, days=1); assert "files" in cleaned

