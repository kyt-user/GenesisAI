import json
import time

import pytest

from genesisai.agent.completion import CompletionValidator
from genesisai.core.prompt.context_budgeter import ContextBudgeter, PROFILES, select_initial_profile
from genesisai.agent.evidence import EvidenceBundle
from genesisai.shared.messages import Response, ToolCall
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.research import ResearchController
from genesisai.agent.runner import Runner
from genesisai.shared.security import Access
from genesisai.core.state.store import CURRENT_SESSION_SCHEMA_VERSION, Store, migrate_session
from genesisai.core.tools.tool_runtime import ToolRuntime


class FakeModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.contexts = []
        self.tools = []

    def chat(self, messages, tools=None, **kwargs):
        self.contexts.append(messages)
        self.tools.append(tools or [])
        value = next(self.responses)
        return value(messages) if callable(value) else value

    def stream_chat(self, messages, tools=None):
        self.contexts.append(messages)
        self.tools.append(tools or [])
        value = next(self.responses)
        result = value(messages) if callable(value) else value
        if isinstance(result, Exception):
            raise result
        if result.content:
            yield result.content
        yield result


def tool_call(name, arguments, call_id="call_1"):
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


@pytest.fixture
def runtime_env(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "outputs"
    store = Store(workspace)
    store.data.update(output=str(output), deadline=time.time() + 300)
    store.save()
    runtime = ToolRuntime(
        store,
        Access([], output, workspace),
        confirm_search=False,
        confirm_writes=False,
        providers=[],
    )
    return store, runtime


def test_p11_profiles_are_fixed_and_conservative():
    assert PROFILES["direct_answer"].model_calls == 3
    assert PROFILES["web_quick"].search_queries == 2
    assert PROFILES["web_quick"].search_fetches == 4
    assert PROFILES["web_quick"].token_budget == 30000
    assert select_initial_profile("请深度、全面调研这个主题") == "web_deep"
    assert select_initial_profile("比较并整理一份专题报告") == "web_normal"
    assert select_initial_profile("你好") == "direct_answer"
    assert select_initial_profile("读取并修改 DOCX 和 XLSX") == "local_files"
    assert select_initial_profile("查询官网目前的售价") == "web_quick"
    assert select_initial_profile("再说一下刚才查到的售价") == "direct_answer"


def test_p11_prompt_composer_is_modular_and_hashed():
    composed = PromptComposer().compose("direct_answer", {"profile": "direct_answer"})
    assert "system/identity.prompt.yaml" in composed.names
    assert "protocols/direct_answer.prompt.yaml" in composed.names
    assert "web_research.md" not in composed.names
    assert composed.hashes and all(len(value) == 64 for value in composed.hashes.values())
    assert "当前日期" in composed.text and "时区" in composed.text


def test_p11_v2_migrates_losslessly_to_v3(tmp_path):
    old = {
        "schema_version": 2,
        "id": "session_legacy",
        "status": "completed",
        "messages": [{"role": "user", "content": "keep"}],
        "calls": {}, "sources": {}, "artifacts": {}, "pending": [],
        "grants": [], "output": str(tmp_path), "remote_allowed": False,
        "run_id": "run_old", "rounds": 1, "tool_count": 0, "failures": 0,
        "deadline": 1, "usage": {"total_tokens": 7}, "history": [],
        "tool_runtime": {"active_tools": ["search_query"]},
    }
    migrated = migrate_session(old)
    assert CURRENT_SESSION_SCHEMA_VERSION == 3
    assert migrated["messages"] == old["messages"]
    assert migrated["tool_runtime"] == {"active_skills": []}
    assert migrated["run_runtime"]["phase"] == "done"


def test_p11_unregistered_and_search_pages_are_rejected(runtime_env):
    store, runtime = runtime_env
    store.data["run_runtime"] = ResearchController.empty_state("web_quick")
    store.save()
    missing = runtime.execute({"id": "f1", "name": "fetch_web_content", "arguments": json.dumps({"url": "https://example.com/a"})})
    search_page = runtime.execute({"id": "f2", "name": "fetch_web_content", "arguments": json.dumps({"url": "https://www.google.com/search?q=x"})})
    assert missing["error"]["code"] == "candidate_not_registered"
    assert search_page["error"]["code"] == "search_result_page_forbidden"


def test_p11_query_registers_candidates_and_fetch_creates_evidence(runtime_env):
    store, runtime = runtime_env

    class Provider:
        name = "fixture"
        def search(self, query, limit=5):
            return [{"title": "Apple", "url": "https://apple.example/news", "snippet": "event"}]

    class Network:
        calls = 0
        def fetch(self, url):
            self.calls += 1
            return {"url": url, "title": "Apple event", "text": "New products " * 800, "truncated": False}

    runtime.providers = [Provider()]
    runtime.network = Network()
    store.data["run_runtime"] = ResearchController.empty_state("web_quick")
    store.save()
    query = runtime.execute({"id": "q1", "name": "fetch_web_content", "arguments": json.dumps({"query": "Apple event"})})
    fetch = runtime.execute({"id": "f1", "name": "fetch_web_content", "arguments": json.dumps({"url": "https://apple.example/news"})})
    assert query["ok"] and store.data["run_runtime"]["candidate_count"] == 1
    assert fetch["ok"] and len(fetch["data"]["text"]) <= 4000
    assert fetch["source_refs"] == store.data["run_runtime"]["evidence_refs"]
    evidence = EvidenceBundle(store).items()[0]
    assert evidence["canonical_url"] == "https://apple.example/news"
    assert evidence["content_hash"] and evidence["status"] == "ready"


def test_p11_direct_answer_dispatches_full_tool_set(runtime_env):
    store, runtime = runtime_env
    model = FakeModel(Response(content="你好，我可以帮你。", finish_reason="stop", usage={"total_tokens": 12}))
    result = Runner(model, runtime).start("你好")
    assert result["status"] == "completed"
    assert len(model.contexts) == 1
    offered = {item["function"]["name"] for item in model.tools[0]}
    assert {"read_files", "editor", "fetch_web_content"}.issubset(offered)
    assert store.data["run_runtime"]["profile"] == "direct_answer"


def test_p11_length_recovers_once_without_tools(runtime_env):
    store, runtime = runtime_env
    model = FakeModel(
        Response(content="已有部分答案", finish_reason="length", usage={"total_tokens": 100}),
        Response(content="完整答案", finish_reason="stop", usage={"total_tokens": 80}),
    )
    result = Runner(model, runtime).start("解释这个概念")
    assert result["status"] == "completed" and result["answer"] == "完整答案"
    assert model.tools[1] == []
    assert store.data["run_runtime"]["partial_response"] == "已有部分答案"
    assert store.data["run_runtime"]["recovery_counts"]["length"] == 1


def test_p11_second_length_delivers_partial(runtime_env):
    _, runtime = runtime_env
    model = FakeModel(
        Response(content="第一部分", finish_reason="length"),
        Response(content="第二部分", finish_reason="length"),
    )
    result = Runner(model, runtime).start("长回答")
    assert result["status"] == "partial"
    assert "第一部分" in result["answer"] and "第二部分" in result["answer"]


def test_p11_context_compacts_old_tool_bodies_and_keeps_previous_answer(runtime_env):
    store, _ = runtime_env
    store.data["messages"] = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "name": "search_fetch", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "x", "content": json.dumps({"data": {"text": "X" * 20000}, "source_refs": ["src_1"]})},
        {"role": "assistant", "content": "上一轮最终答案"},
        {"role": "user", "content": "它的优势是什么"},
    ]
    messages = ContextBudgeter(store, PromptComposer()).build("direct_answer")
    joined = "".join(message.content or "" for message in messages)
    assert "上一轮最终答案" in joined
    assert len(joined) < 20000


def test_p11_completion_decisions_are_three_state():
    validator = CompletionValidator()
    assert validator.decide({"phase": "explore", "evidence_refs": [], "stop_reason": None}) == "continue"
    assert validator.decide({"phase": "answer", "evidence_refs": [], "stop_reason": "budget"}) == "answer"
    assert validator.decide({"phase": "recover", "evidence_refs": [], "stop_reason": None}) == "recover"

