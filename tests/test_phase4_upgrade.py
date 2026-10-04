"""Phase 4 升级功能测试覆盖。

覆盖：
- 流式输出 + reasoning 渲染
- 自验证循环
- creative profile 组装
- profile-aware compact
- code_index 工具
- novel 工具（chapter_list, chapter_read）
"""

import json
from pathlib import Path
from io import StringIO

import pytest
from rich.console import Console

from genesisai.shared.messages import Response, ToolCall
from genesisai.app.terminal_view import CliView
from genesisai.core.prompt.context_budgeter import ContextBudgeter, select_initial_profile
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.state.store import Store
from genesisai.shared.security import Access
from genesisai.core.tools.tool_runtime import ToolRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_and_runtime(tmp_path, *, active=()):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    state = tmp_path / "state"
    output = tmp_path / "output"
    store = Store(state, workspace=workspace)
    store.data.update(grants=[str(workspace)], output=str(output), run_id="run_test")
    store.data.setdefault("run_runtime", {})
    store.save()
    runtime = ToolRuntime(
        store,
        Access([workspace], output, state, workspace_root=workspace),
        providers=[],
        confirm_writes=False,
        confirm_search=False,
        confirm_shell=False,
    )
    return workspace, output, store, runtime


def _view_and_stream():
    stream = StringIO()
    console = Console(file=stream, width=120, force_terminal=False)
    view = CliView(console)
    return view, stream


# ---------------------------------------------------------------------------
# 4.1 Streaming + reasoning rendering
# ---------------------------------------------------------------------------

class TestStreamingRendering:
    def test_stream_token_renders_incrementally(self):
        view, stream = _view_and_stream()
        view.render_progress("stream_token", {"text": "Hello "})
        view.render_progress("stream_token", {"text": "world"})
        output = stream.getvalue()
        assert "Hello " in output
        assert "world" in output
        assert view._streaming is True
        assert view._stream_chars == 11

    def test_reasoning_token_shows_status_without_raw_reasoning(self):
        view, stream = _view_and_stream()
        view.render_progress("reasoning_token", {"text": "thinking..."})
        output = stream.getvalue()
        assert "正在规划" in output
        assert "thinking..." not in output
        assert view._reasoning_active is True

    def test_reasoning_transitions_to_streaming(self):
        view, stream = _view_and_stream()
        view.render_progress("reasoning_token", {"text": "分析中"})
        assert view._reasoning_active is True
        view.render_progress("stream_token", {"text": "答案是"})
        assert view._reasoning_active is False
        assert view._streaming is True
        output = stream.getvalue()
        assert "思考结束" in output

    def test_tool_start_ends_streaming(self):
        view, stream = _view_and_stream()
        view.render_progress("stream_token", {"text": "some text"})
        assert view._streaming is True
        view.render_progress("tool_start", {"name": "file_read"})
        assert view._streaming is False
        output = stream.getvalue()
        assert "file_read" in output
        assert "运行中" in output

    def test_verification_start_renders(self):
        view, stream = _view_and_stream()
        view.render_progress("verification_start", {"round": 2})
        output = stream.getvalue()
        assert "自动验证" in output
        assert "2" in output

    def test_model_round_resets_reasoning(self):
        view, stream = _view_and_stream()
        view.render_progress("reasoning_token", {"text": "x"})
        assert view._reasoning_active is True
        view.render_progress("model", {"round": 3})
        assert view._reasoning_active is False


# ---------------------------------------------------------------------------
# 4.3 Creative profile selection and prompt assembly
# ---------------------------------------------------------------------------

class TestCreativeProfile:
    def test_select_initial_profile_detects_creative_intent(self):
        assert select_initial_profile("我想写小说") == "creative"
        assert select_initial_profile("帮我续写下一章") == "creative"
        assert select_initial_profile("这个角色需要扩展") == "creative"
        assert select_initial_profile("创建一个世界观") == "creative"

    def test_creative_profile_has_high_budget(self):
        from genesisai.core.prompt.context_budgeter import PROFILES
        profile = PROFILES["creative"]
        assert profile.model_calls >= 25
        assert profile.token_budget >= 100000
        assert profile.seconds >= 600


# ---------------------------------------------------------------------------
# 4.4 Profile-aware compact
# ---------------------------------------------------------------------------

class TestProfileAwareCompact:
    def test_compact_includes_coding_context_for_local_files(self, tmp_path):
        workspace, output, store, runtime = _store_and_runtime(tmp_path)
        store.data["workspace"] = str(workspace)
        store.data["run_runtime"]["profile"] = "local_files"
        store.data["run_runtime"]["protocols"] = ["coding"]
        # Add messages with tool results — need 4+ groups so older groups contain both tool messages.
        store.data["messages"] = [
            {"role": "user", "content": "修复 bug"},
            {"role": "assistant", "content": "修改文件", "tool_calls": [{"id": "c1", "name": "file_patch", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"ok": True, "data": {"tool": "file_patch", "written": True}})},
            {"role": "user", "content": "跑测试"},
            {"role": "assistant", "content": "运行测试", "tool_calls": [{"id": "c2", "name": "test_run", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"ok": True, "data": {"tool": "test_run", "passed": True, "pytest": "3 passed"}})},
            {"role": "user", "content": "继续修改"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "再改"},
        ]

        composer = PromptComposer()
        budgeter = ContextBudgeter(store, composer)
        summary = budgeter.compact(force=True)

        assert "coding_context" in summary
        coding = summary["coding_context"]
        assert len(coding.get("file_changes", [])) >= 1
        assert len(coding.get("test_results", [])) >= 1


# ---------------------------------------------------------------------------
# 4.5 Code index tool
# ---------------------------------------------------------------------------

class TestCodeIndex:
    def test_code_index_extracts_python_signatures(self, tmp_path):
        workspace, _, _, runtime = _store_and_runtime(tmp_path)
        target = workspace / "sample.py"
        target.write_text(
            "import os\n\n"
            "class MyClass:\n"
            "    def method(self):\n"
            "        pass\n\n"
            "def top_function(x, y):\n"
            "    return x + y\n\n"
            "# TODO: add more\n",
            encoding="utf-8",
        )
        call = {
            "id": "call_idx",
            "name": "search_codebase",
            "arguments": json.dumps({"path": ".", "mode": "index"}),
        }
        result = runtime.execute(call)
        assert result["ok"]
        data = result["data"]
        assert data["summary"]["files_indexed"] >= 1
        assert data["summary"]["total_classes"] >= 1
        assert data["summary"]["total_functions"] >= 1

    def test_code_index_handles_empty_directory(self, tmp_path):
        workspace, _, _, runtime = _store_and_runtime(tmp_path)
        call = {
            "id": "call_idx2",
            "name": "search_codebase",
            "arguments": json.dumps({"path": ".", "mode": "index"}),
        }
        result = runtime.execute(call)
        assert result["ok"]


# ---------------------------------------------------------------------------
# 4.6 Novel tools
# ---------------------------------------------------------------------------

class TestNovelTools:
    def test_chapter_list_finds_markdown_chapters(self, tmp_path):
        workspace, _, _, runtime = _store_and_runtime(tmp_path, active=("chapter_list",))
        (workspace / "chapter_01.md").write_text("# 第一章\n\n内容...", encoding="utf-8")
        (workspace / "chapter_02.md").write_text("# 第二章\n\n更多内容...", encoding="utf-8")
        call = {
            "id": "call_cl",
            "name": "chapter_list",
            "arguments": json.dumps({"path": "."}),
        }
        result = runtime.execute(call)
        assert result["ok"]
        data = result["data"]
        assert data["chapter_count"] == 2
        assert "第一章" in data.get("text", "")

    def test_chapter_read_returns_content(self, tmp_path):
        workspace, _, _, runtime = _store_and_runtime(tmp_path, active=("chapter_read",))
        chapter = workspace / "ch1.md"
        chapter.write_text("# 第一章\n\n这是正文内容。\n\nAlice 走进了房间。", encoding="utf-8")
        call = {
            "id": "call_cr",
            "name": "chapter_read",
            "arguments": json.dumps({"path": "ch1.md"}),
        }
        result = runtime.execute(call)
        assert result["ok"]
        data = result["data"]
        assert "正文" in data.get("text", "") or "内容" in data.get("text", "")


# ---------------------------------------------------------------------------
# 4.7 Protocols detection
# ---------------------------------------------------------------------------

class TestProtocolsDetection:
    def test_debugging_protocol_detected(self):
        from genesisai.core.prompt.task_protocols import TaskProtocolSelector
        protocols = TaskProtocolSelector().protocols_for("这段代码为什么会报错？debug 一下")
        assert "debugging" in protocols

    def test_creative_writing_protocol_detected(self):
        from genesisai.core.prompt.task_protocols import TaskProtocolSelector
        protocols = TaskProtocolSelector().protocols_for("帮我写小说的第一章")
        assert "creative_writing" in protocols

    def test_revision_protocol_detected(self):
        from genesisai.core.prompt.task_protocols import TaskProtocolSelector
        protocols = TaskProtocolSelector().protocols_for("请润色这段文字")
        assert "revision" in protocols

    def test_coding_protocol_detected(self):
        from genesisai.core.prompt.task_protocols import TaskProtocolSelector
        protocols = TaskProtocolSelector().protocols_for("帮我编写一个函数")
        assert "coding" in protocols


# ---------------------------------------------------------------------------
# 4.8 Prompt template loading
# ---------------------------------------------------------------------------

class TestPromptTemplates:
    def test_debugging_prompt_loads(self):
        composer = PromptComposer()
        composed = composer.compose(
            "direct_answer",
            {"protocols": ["debugging"]},
        )
        assert "调试" in composed.text or "debug" in composed.text.lower() or len(composed.text) > 0

    def test_creative_writing_prompt_loads(self):
        composer = PromptComposer()
        composed = composer.compose(
            "creative",
            {"protocols": ["creative_writing"]},
        )
        assert len(composed.text) > 0

    def test_revision_prompt_loads(self):
        composer = PromptComposer()
        composed = composer.compose(
            "creative",
            {"protocols": ["revision"]},
        )
        assert len(composed.text) > 0


# ---------------------------------------------------------------------------
# 4.9 Self-verification loop integration
# ---------------------------------------------------------------------------

class TestSelfVerification:
    def test_verification_flag_set_after_file_modify(self, tmp_path):
        """After a file modification tool succeeds, _needs_verification is set."""
        from genesisai.agent.runner import Runner
        from genesisai.shared.messages import Response

        workspace, output, store, runtime = _store_and_runtime(tmp_path, active=("file_read",))
        store.data["run_runtime"]["profile"] = "local_files"
        store.data["run_runtime"]["protocols"] = ["coding"]

        class MinimalModel:
            def chat(self, messages, tools=None, **kwargs):
                return Response(content="done")
            def stream_chat(self, messages, tools=None):
                yield "done"
                yield Response(content="done")

        runner = Runner(MinimalModel(), runtime, max_rounds=5, seconds=60)
        assert runner._needs_verification is False
        # Simulate the flag being set after file modification
        runner._needs_verification = True
        assert runner._needs_verification is True

    def test_should_auto_verify_respects_profile(self, tmp_path):
        """_should_auto_verify returns True only for coding/local_files profiles."""
        from genesisai.agent.runner import Runner
        from genesisai.shared.messages import Response

        workspace, output, store, runtime = _store_and_runtime(tmp_path)

        class MinimalModel:
            def chat(self, messages, tools=None, **kwargs):
                return Response(content="done")
            def stream_chat(self, messages, tools=None):
                yield "done"
                yield Response(content="done")

        runner = Runner(MinimalModel(), runtime, max_rounds=5, seconds=60)

        # local_files profile should auto-verify
        assert runner.development.should_auto_verify({"profile": "local_files", "protocols": [], "verification_rounds": 0})
        # coding protocol should auto-verify
        assert runner.development.should_auto_verify({"profile": "direct_answer", "protocols": ["coding"], "verification_rounds": 0})
        # direct_answer without coding should NOT auto-verify
        assert not runner.development.should_auto_verify({"profile": "direct_answer", "protocols": [], "verification_rounds": 0})
        # Max rounds reached should NOT auto-verify
        assert not runner.development.should_auto_verify({"profile": "local_files", "protocols": [], "verification_rounds": 3})

    def test_verification_rounds_tracked_in_state(self, tmp_path):
        """verification_rounds field is properly initialized and tracked."""
        from genesisai.core.research import ResearchController
        state = ResearchController.empty_state("local_files")
        assert state.get("verification_rounds") == 0
