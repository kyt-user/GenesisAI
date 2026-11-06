import json
import time

import pytest

from genesisai.agent.completion import CompletionValidator
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.shared.budgets import PROFILES
from genesisai.core.prompt.task_protocols import select_initial_profile
from genesisai.agent.evidence import EvidenceBundle
from genesisai.shared.messages import Response, ToolCall
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.research import ResearchController
from genesisai.agent.loop import Runner
from genesisai.shared.security import Access
from genesisai.core.persistence.session import Session
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

    def stream_chat(self, messages, tools=None, **kwargs):
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
    store = Session(tmp_path / "app", workspace=workspace)
    store.record.apply(output=str(output), deadline=time.time() + 300)
    store.commit()
    runtime = ToolRuntime(
        store,
        Access([], output, store.root, workspace_root=workspace),
        permission_mode="auto",
        providers=[],
    )
    return store, runtime


def test_p11_profiles_are_fixed_and_conservative():
    assert PROFILES["direct_answer"].model_calls == 3
    assert PROFILES["web_quick"].search_queries == 2
    assert PROFILES["web_quick"].search_fetches == 4
    assert "token_budget" not in PROFILES["web_quick"].to_dict()
    assert select_initial_profile("请深度、全面调研这个主题") == "web_deep"
    assert select_initial_profile("比较并整理一份专题报告") == "web_normal"
    assert select_initial_profile("你好") == "direct_answer"
    assert select_initial_profile("读取并修改 DOCX 和 XLSX") == "local_files"
    assert select_initial_profile("查询官网目前的售价") == "web_quick"


def test_mcp_structured_links_register_candidates_without_promoting_snippets(runtime_env):
    from genesisai.core.extensions.mcp.bridge import McpBridge

    store, runtime = runtime_env
    url = "https://example.org/item"
    bridge = McpBridge(store.workspace, runtime.registry, runtime.loader)
    result = bridge._content("news", "search", {
        "content": [{"type": "text", "text": "USD 9, https://in-prose.example/"}],
        "structuredContent": {"items": [
            {"title": "summary", "links": {"item": url + "?utm_source=test", "original": url}},
            {"links": {"private": "http://127.0.0.1/", "invalid": "https://user:pass@example.org/"}},
            {"url": "file:///C:/secret"}, {"links": []}, None,
        ]},
    })
    call = {"id": "m1", "name": "mcp_news_search", "arguments": "{}"}
    runtime.research.after_execute(call, {"ok": False, "data": result})
    assert not store.record.run_runtime["candidates"]
    runtime.research.after_execute(dict(call, id="m2"), {"ok": True, "data": result})
    state = store.record.run_runtime
    assert list(state["candidates"]) == [url]
    assert state["candidates"][url]["query_call_id"] == "m2"
    assert state["candidates"][url]["tool_name"] == "mcp_news_search"
    assert not state["evidence_refs"] and not store.record.sources
    assert select_initial_profile("再说一下刚才查到的售价") == "direct_answer"


def test_p11_prompt_composer_is_modular_and_hashed():
    composed = PromptComposer().compose("direct_answer", {"profile": "direct_answer"})
    assert "system/identity.prompt.yaml" in composed.names
    assert "protocols/direct_answer.prompt.yaml" in composed.names
    assert "web_research.md" not in composed.names
    assert composed.hashes and all(len(value) == 64 for value in composed.hashes.values())
    assert "当前日期" in composed.text and "时区" in composed.text


def test_p11_unregistered_and_search_pages_are_rejected(runtime_env):
    store, runtime = runtime_env
    store.record.run_runtime = ResearchController.empty_state("web_quick")
    store.commit()
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
    store.record.run_runtime = ResearchController.empty_state("web_quick")
    store.commit()
    query = runtime.execute({"id": "q1", "name": "fetch_web_content", "arguments": json.dumps({"query": "Apple event"})})
    fetch = runtime.execute({"id": "f1", "name": "fetch_web_content", "arguments": json.dumps({"url": "https://apple.example/news"})})
    assert query['ok'] and store.record.run_runtime['candidate_count'] == 1
    assert fetch["ok"] and len(fetch["data"]["text"]) <= 4000
    assert fetch['source_refs'] == store.record.run_runtime['evidence_refs']
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
    assert store.record.run_runtime['profile'] == 'direct_answer'


def test_p11_length_recovers_once_without_tools(runtime_env):
    store, runtime = runtime_env
    model = FakeModel(
        Response(content="已有部分答案", finish_reason="length", usage={"total_tokens": 100}),
        Response(content="完整答案", finish_reason="stop", usage={"total_tokens": 80}),
    )
    result = Runner(model, runtime).start("解释这个概念")
    assert result["status"] == "completed" and result["answer"] == "完整答案"
    assert model.tools[1] == []
    assert store.record.run_runtime['partial_response'] == '已有部分答案'
    assert store.record.run_runtime['recovery_counts']['length'] == 1


def test_p11_second_length_delivers_partial(runtime_env):
    _, runtime = runtime_env
    model = FakeModel(
        Response(content="第一份草稿。", finish_reason="length"),
        Response(content="重写后的完整句子。尚未写完的", finish_reason="length"),
    )
    result = Runner(model, runtime).start("长回答")
    assert result["status"] == "partial"
    assert "第一份草稿" not in result["answer"] and "重写后的完整句子。" in result["answer"]
    assert "尚未写完的" not in result["answer"]


def test_p11_context_compacts_old_tool_bodies_and_keeps_previous_answer(runtime_env):
    store, _ = runtime_env
    store.record.messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "name": "search_fetch", "arguments": "{}"}]},
        {"role": "tool", "tool_call_id": "x", "content": json.dumps({"data": {"text": "X" * 20000}, "source_refs": ["src_1"]})},
        {"role": "assistant", "content": "上一轮最终答案"},
        {"role": "user", "content": "它的优势是什么"},
    ]
    budgeter = ContextBudgeter(store, PromptComposer(), config={"max_input_tokens": 9000, "strategy": "basic"})
    messages = budgeter.prepare(budgeter.build("direct_answer"), [])
    joined = "".join(message.content or "" for message in messages)
    assert "上一轮最终答案" in joined
    assert len(joined) < 20000


def test_p11_completion_decisions_are_three_state():
    validator = CompletionValidator()
    assert validator.decide({"phase": "explore", "evidence_refs": [], "stop_reason": None}) == "continue"
    assert validator.decide({"phase": "answer", "evidence_refs": [], "stop_reason": "budget"}) == "answer"
    assert validator.decide({"phase": "recover", "evidence_refs": [], "stop_reason": None}) == "recover"

