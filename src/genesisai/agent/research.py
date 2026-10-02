"""网络研究的候选、预算、去重、重试与停止控制。"""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from genesisai.prompt.context_budgeter import PROFILES
from genesisai.agent.evidence import EvidenceBundle
from genesisai.runtime.executor import error_result
from genesisai.capabilities.web.contracts import canonicalize_url, normalized_text


URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
SEARCH_HOSTS = frozenset({
    "google.com", "www.google.com", "bing.com", "www.bing.com",
    "duckduckgo.com", "www.duckduckgo.com", "search.brave.com",
})
NON_RETRYABLE = frozenset({
    "unsafe_url", "unsupported_format", "unsupported_encoding", "no_text",
    "size_limit", "redirect_limit", "unsafe_redirect", "candidate_not_registered",
    "search_result_page_forbidden", "domain_limit", "duplicate_content",
})


class ResearchController:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def empty_state(profile: str = "direct_answer", *, phase: str = "explore") -> dict:
        selected = PROFILES[profile]
        return {
            "profile": profile,
            "phase": phase,
            "lifecycle": "ready" if phase == "explore" else ("complete" if phase == "done" else "recover"),
            "state_history": [],
            "protocols": [],
            "context_summary": None,
            "context_report": {},
            "budget": selected.to_dict(),
            "usage": {
                "model_calls": 0, "tool_searches": 0, "tool_loads": 0,
                "search_queries": 0, "search_fetches": 0,
                "observations": 0, "total_tokens": 0,
            },
            "candidates": {},
            "candidate_count": 0,
            "fetched_count": 0,
            "content_hashes": [],
            "evidence": {},
            "evidence_refs": [],
            "failed_resources": {},
            "recovery_counts": {"length": 0, "empty": 0, "tool_protocol": 0, "provider": 0},
            "counted_calls": [],
            "completed_queries": [],
            "progress_mark": [0, 0],
            "stalled_rounds": 0,
            "stop_reason": None,
            "partial_response": "",
            "last_tool_name": None,
            "last_query_relevant": False,
            "reuse_evidence": False,
            "allow_new_network": False,
            "confirmation_paused_at": None,
            "paused_seconds": 0,
            "last_failure": None,
            "observed_spans": [],
            "tool_activity": [],
            "search_diagnostics": {},
            "prompt_names": [],
            "prompt_hashes": {},
            "verification_rounds": 0,
            "verification_required": False,
            "started_at": time.time(),
        }

    @property
    def state(self) -> dict:
        return self.store.data["run_runtime"]

    def switch_profile(self, profile: str) -> None:
        current = self.state["profile"]
        if current == profile:
            return
        self.state["profile"] = profile
        self.state["budget"] = PROFILES[profile].to_dict()
        if 'execution_seconds_limit' in self.state:
            self.store.data['deadline'] = self.state['started_at'] + self.state.get('paused_seconds', 0) + min(
                self.state['execution_seconds_limit'], self.state['budget']['seconds'])
        self.store.save()

    def register_user_urls(self, text: str) -> None:
        for raw in URL_PATTERN.findall(text):
            try:
                self._register_candidate(raw.rstrip(".,;!?，。；！？"), source_type="user_url")
            except Exception:
                continue
        self.store.save()

    def before_execute(self, call: dict) -> dict | None:
        name = call.get("name")
        try:
            args = json.loads(call.get("arguments", ""))
        except Exception:
            return None
        if name in {"search_query", "search_fetch"} and self.state["profile"] == "direct_answer":
            self.switch_profile("web_quick")
        if name == "tool_load" and isinstance(args, dict):
            names = args.get("names") or []
            if any(str(item).startswith("search_") for item in names):
                self.switch_profile("web_quick")
            elif any(str(item).startswith("file_") for item in names):
                self.switch_profile("local_files")
        # 缓存读取和无效 URL 不消耗实际网络预算。
        if name == "search_fetch" and isinstance(args, dict) and isinstance(args.get("url"), str):
            checked = self._check_fetch(call, args["url"])
            if checked is not None:
                return checked
        budget_key = {
            "tool_search": "tool_searches", "tool_load": "tool_loads",
            "search_query": "search_queries", "search_fetch": "search_fetches",
        }.get(name)
        if budget_key and self.state["usage"][budget_key] >= self.state["budget"][budget_key]:
            if budget_key not in {"tool_searches", "tool_loads"}:
                self.force_answer(f"{budget_key}_budget")
            return error_result(call, "budget_exhausted", f"已达到 {budget_key} 硬预算")
        if name == "search_query" and isinstance(args, dict) and isinstance(args.get("query"), str):
            query = normalized_text(args["query"]).casefold()
            if query in self.state["completed_queries"]:
                return error_result(call, "duplicate_query", "相同查询已经执行；请使用现有候选和证据回答")
        if name == "search_fetch" and isinstance(args, dict) and isinstance(args.get("url"), str):
            return self._check_fetch(call, args["url"])
        return None

    def after_execute(self, call: dict, result: dict) -> dict:
        if result.get("pending"):
            return result
        call_id = call.get("id")
        cached = bool((result.get("data") or {}).get("cached"))
        ledger = self.store.data['calls'].get(call_id, {})
        executed = ledger.get('executed', False)
        if call_id not in self.state["counted_calls"]:
            self.state["counted_calls"].append(call_id)
            name = call.get("name")
            counter = {
                "tool_search": "tool_searches", "tool_load": "tool_loads",
                "search_query": "search_queries", "search_fetch": "search_fetches",
            }.get(name)
            if counter and not cached and (name not in {"search_query", "search_fetch"} or executed):
                self.state["usage"][counter] += 1
            if cached:
                self.state["usage"]["cache_hits"] = self.state["usage"].get("cache_hits", 0) + 1
        name = call.get("name")
        self.state["last_tool_name"] = name
        if name == "search_query":
            self._after_query(call, result)
        elif name == "search_fetch":
            self._after_fetch(call, result)
        self._check_hard_limits()
        self.store.save()
        return result

    def note_model(self, tokens: int) -> None:
        self.state["usage"]["model_calls"] += 1
        self.state["usage"]["total_tokens"] += max(tokens, 0)
        self._check_hard_limits()
        self.store.save()

    def note_progress_round(self) -> None:
        if self.state["profile"] not in {"web_quick", "web_normal", "web_deep"}:
            return
        if self.state["usage"].get("search_queries", 0) == 0 and self.state["usage"].get("search_fetches", 0) == 0:
            return
        mark = [self.state["candidate_count"], len(self.state["evidence_refs"]), self.state['usage']['observations']]
        if mark == self.state.get("progress_mark"):
            self.state["stalled_rounds"] += 1
        else:
            self.state["stalled_rounds"] = 0
            self.state["progress_mark"] = mark
        if self.state["stalled_rounds"] >= 3:
            self.force_answer("no_new_evidence")
        self.store.save()

    def should_force_answer(self) -> bool:
        # 相关性和字段覆盖属于当前问题，而非品牌。
        return self.state["phase"] == "answer" or bool(self.state.get("stop_reason"))

    def force_answer(self, reason: str) -> None:
        if self.state.get("phase") not in {"recover", "done"}:
            self.state["phase"] = "answer"
        self.state["stop_reason"] = self.state.get("stop_reason") or reason

    def remaining(self) -> dict:
        budget, usage = self.state["budget"], self.state["usage"]
        return {
            key: max(0, budget[key] - usage.get(key, 0))
            for key in ("model_calls", "tool_searches", "tool_loads", "search_queries", "search_fetches", "token_budget")
        }

    def _check_fetch(self, call: dict, raw_url: str) -> dict | None:
        try:
            url = canonicalize_url(raw_url)
        except Exception:
            return error_result(call, "unsafe_url", "URL 不是允许的 HTTP(S) 公网地址")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in SEARCH_HOSTS or (host.endswith(".google.com") and parts.path.startswith("/search")):
            return error_result(call, "search_result_page_forbidden", "禁止抓取搜索引擎结果页；请读取已登记的正文候选")
        candidate = self.state["candidates"].get(url)
        if not candidate:
            return error_result(call, "candidate_not_registered", "URL 未登记。请使用已有候选，或先 search_query 搜索目标页面，再抓取返回的 URL；批准不能替代登记。")
        if candidate.get("status") == "fetched":
            evidence = next((item for item in self.state["evidence"].values() if item["canonical_url"] == url), None)
            if evidence:
                args = json.loads(call['arguments'])
                offset, limit = args.get('offset', 0), args.get('limit', 4000)
                page_path = self.store.root / 'sources' / (evidence['source_ref'] + '.json')
                page = json.loads(page_path.read_text(encoding='utf-8'))
                text = page['text']
                return {
                    "call_id": call.get("id", ""), "ok": True,
                    "data": {
                        "text": text[offset:offset + limit], "url": url, "title": evidence["title"],
                        "source_refs": [evidence["source_ref"]], "truncated": bool(page.get('truncated')) or len(text) > offset + limit,
                        "next_offset": offset + limit if len(text) > offset + limit else None, "cached": True, "duplicate": True,
                        "links": [link for link in page.get('links', []) if isinstance(link, dict) and link.get('url') in self.state['candidates']],
                    },
                    "error": None, "source_refs": [evidence["source_ref"]],
                    "artifact_refs": [], "truncated": bool(page.get('truncated')) or len(text) > offset + limit,
                }
        failure = self.state["failed_resources"].get(url)
        if failure:
            if not failure.get("retryable") or failure.get("attempts", 0) >= 2:
                return error_result(call, "retry_exhausted", "该候选已经失败且不允许再次重试")
        return None

    def _after_query(self, call: dict, result: dict) -> None:
        try:
            args = json.loads(call["arguments"])
            query = normalized_text(args["query"]).casefold()
        except Exception:
            query = ""
        if not result.get("ok"):
            ledger = self.store.data['calls'].get(call.get('id'), {})
            if query and ledger.get('executed') and query not in self.state["completed_queries"]:
                self.state["completed_queries"].append(query)
            diagnostics = self.state.get('search_diagnostics') or {}
            if diagnostics.get('all_providers_failed') and self._all_candidates_exhausted():
                self.force_answer('search_service_unavailable')
            return
        if result.get("ok"):
            before = set(self.state['candidates'])
            if query and query not in self.state["completed_queries"]:
                self.state["completed_queries"].append(query)
            for hit in (result.get("data") or {}).get("hits", []):
                value = hit.to_dict() if hasattr(hit, "to_dict") else hit
                if not isinstance(value, dict) or not value.get("url"):
                    continue
                try:
                    self._register_candidate(
                        value["url"], source_type="search",
                        title=str(value.get("title", "")), snippet=str(value.get("snippet", "")),
                        query_call_id=call.get("id"), published_at=value.get("published_at"),
                    )
                except Exception:
                    continue
            data = result.get('data') or {}
            added = len(set(self.state['candidates']) - before)
            diagnostics = {
                'provider': data.get('provider'), 'fallback_errors': data.get('fallback_errors', []),
                'new_candidates': added, 'repeated_candidates': added == 0 and bool(data.get('hits')),
            }
            self.state['search_diagnostics'] = diagnostics
            data['diagnostics'] = diagnostics
            if diagnostics['repeated_candidates']:
                data['guidance'] = '本次搜索没有新增候选。不要把重复首页当作目标页；优先读取现有相关候选或改用精确产品名和站点限定。仍无可用目标页时简洁说明检索未找到，勿要求用户提供内部预算。'
                if self._all_candidates_exhausted():
                    self.force_answer('external_source_unavailable')
            self.store.event('search_diagnostics', provider=diagnostics['provider'], new_candidates=added,
                             repeated_candidates=diagnostics['repeated_candidates'],
                             provider_errors=diagnostics['fallback_errors'])

    def _after_fetch(self, call: dict, result: dict) -> None:
        data = result.get('data') or {}
        if result.get('ok') and isinstance(data.get('text'), str):
            span = hashlib.sha256((str(data.get('url')) + '\n' + data['text']).encode('utf-8')).hexdigest()
            spans = self.state.setdefault('observed_spans', [])
            if span not in spans:
                spans.append(span)
                self.state['usage']['observations'] += 1
        if data.get('cached'):
            return
        try:
            requested = canonicalize_url(json.loads(call["arguments"])["url"])
        except Exception:
            return
        candidate = self.state["candidates"].get(requested)
        if not result.get("ok"):
            # 预检拒绝（例如 retry_exhausted）没有触及网络，
            # 不能覆盖上次实际执行的失败记录。
            if not self.store.data['calls'].get(call.get('id'), {}).get('executed', False):
                return
            error = result.get("error") or {}
            code = error.get("code", "execution_error")
            message = error.get("message", "")
            retryable = bool(error.get("retryable")) and code not in NON_RETRYABLE
            if "HTTP 403" in message or "HTTP 404" in message:
                retryable = False
            previous = self.state["failed_resources"].get(requested, {})
            status_match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.I)
            attempts = previous.get("attempts", 0) + 1
            self.state["failed_resources"][requested] = {
                "code": code, "retryable": retryable, "attempts": attempts,
                "reason": "http_status" if status_match else "tool_failure",
                **({"http_status": int(status_match.group(1))} if status_match else {}),
            }
            if candidate:
                candidate["status"] = "retryable" if retryable and attempts < 2 else "failed"
            if self._all_candidates_exhausted() and self.state['usage'].get('search_queries', 0) >= self.state['budget'].get('search_queries', 0):
                self.force_answer('external_source_unavailable')
            return
        data = result.get("data") or {}
        refs = result.get("source_refs") or data.get("source_refs") or []
        if not refs or not isinstance(data.get("text"), str):
            return
        final_url = canonicalize_url(data.get("url") or requested)
        if final_url != requested and final_url not in self.state["candidates"]:
            self.state["candidates"][final_url] = {**(candidate or {}), "canonical_url": final_url, "source_type": "redirect"}
        source = self.store.data["sources"].get(refs[0], {})
        depth = (candidate or {}).get('depth', 0)
        registered_links = []
        if depth < 2:
            for link in data.get('links', [])[:40]:
                if not isinstance(link, dict):
                    continue
                try:
                    linked_url = canonicalize_url(link.get('url', ''))
                    if urlsplit(linked_url)[:2] != urlsplit(final_url)[:2]:
                        continue
                    self._register_candidate(linked_url, source_type='page_link', title=str(link.get('title', ''))[:100],
                                             parent_source_ref=refs[0], parent_url=final_url, depth=depth + 1)
                    registered_links.append(link)
                except (ValueError, RuntimeError):
                    continue
        data['links'] = registered_links
        full_hash = source.get("sha256")
        if full_hash and full_hash in self.state["content_hashes"]:
            if candidate:
                candidate["status"] = "duplicate"
            return
        item = EvidenceBundle(self.store).add(
            source_ref=refs[0], canonical_url=final_url,
            title=str(data.get("title", "")), snippet=data["text"],
            published_at=(candidate or {}).get("published_at"),
            content_hash=full_hash,
        )
        if candidate:
            candidate["status"] = "fetched"
            candidate["source_ref"] = item["source_ref"]
        self.state["fetched_count"] += 1

    def _all_candidates_exhausted(self) -> bool:
        """当所有已注册候选都在重试边界内无法抓取时返回 True。"""
        candidates = list(self.state.get('candidates', {}).values())
        if not candidates:
            return True
        for candidate in candidates:
            status = candidate.get('status', 'candidate')
            if status == 'candidate':
                return False
            if status == 'retryable':
                failure = self.state.get('failed_resources', {}).get(candidate.get('canonical_url'), {})
                if failure.get('retryable') and failure.get('attempts', 0) < 2:
                    return False
        return True

    def _register_candidate(self, raw_url: str, *, source_type: str, **metadata) -> str:
        url = canonicalize_url(raw_url)
        host = urlsplit(url).hostname or ""
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError("non-public address")
        except ValueError as exc:
            if exc.args == ("non-public address",):
                raise
        if url not in self.state["candidates"]:
            self.state["candidates"][url] = {
                "canonical_url": url, "domain": host.lower(), "source_type": source_type,
                "status": "candidate", **metadata,
            }
            self.state["candidate_count"] = len(self.state["candidates"])
        # A previously guessed URL becomes usable once actual search/user evidence
        # establishes its provenance; this is not a retry of a failed HTTP request.
        if self.state['failed_resources'].get(url, {}).get('code') == 'candidate_not_registered':
            self.state['failed_resources'].pop(url, None)
        return url

    def _check_hard_limits(self) -> None:
        budget, usage = self.state["budget"], self.state["usage"]
        checks = (
            ("model_calls", "model_budget"), ("tool_searches", "tool_search_budget"),
            ("tool_loads", "tool_load_budget"), ("search_queries", "search_query_budget"),
            ("search_fetches", "search_fetch_budget"),
        )
        for key, reason in checks:
            if key in {"tool_searches", "tool_loads"}:
                continue
            if key == "search_queries":
                # 搜索耗尽不会耗尽独立的抓取预算。
                continue
            if key == "model_calls" and self.state["profile"] == "direct_answer" and self.state.get("counted_calls"):
                # 只有普通无工具问题受 direct_answer 的三次模型硬限；一旦
                # Agent 已进入目录/业务工具流程，先让它完成一次确定性加载。
                continue
            if usage[key] >= budget[key] and budget[key] > 0:
                self.force_answer(reason)
                return
        if usage["total_tokens"] >= int(budget["token_budget"] * 0.75) and self.state["phase"] == "explore":
            self.force_answer("exploration_token_reserve")
        if time.time() - self.state["started_at"] - self.state.get("paused_seconds", 0) >= budget["seconds"]:
            self.force_answer("profile_deadline")

