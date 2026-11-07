"""网络研究的候选、预算、去重、重试与停止控制。"""

from __future__ import annotations

import ipaddress
import hashlib
import json
import re
from urllib.parse import urlsplit

from genesisai.shared.budgets import PROFILES, initial_run_runtime
from genesisai.shared.errors import SearchError
from genesisai.shared.security import ToolError
from genesisai.agent.evidence import EvidenceBundle
from genesisai.shared.results import error_result
from genesisai.shared.web import canonicalize_url, normalized_text
from genesisai.core.extensions.tools.web.source_pages import page_source


FETCH_TOOLS = frozenset({'fetch_web_content', 'fetch_private_resource', 'render_web_page'})


def fetch_canonicalizer(name):
    if name == 'fetch_private_resource':
        from genesisai.core.extensions.tools.web.private_network import canonicalize_private_url
        return canonicalize_private_url
    return canonicalize_url


URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
NON_RETRYABLE = frozenset({
    "unsafe_url", "unsupported_format", "unsupported_encoding",
    "size_limit", "redirect_limit", "invalid_response",
})


def _web_intent(name: str, args: object) -> str | None:
    """把内核网络工具的调用归类为检索(query)或抓取(fetch)。"""
    if name not in FETCH_TOOLS or not isinstance(args, dict):
        return None
    if isinstance(args.get("url"), str):
        return "fetch"
    if isinstance(args.get("query"), str):
        return "query"
    return None


class ResearchController:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def empty_state(profile: str = "direct_answer", *, phase: str = "explore") -> dict:
        return initial_run_runtime(profile, phase=phase)

    @property
    def state(self) -> dict:
        return self.store.record.run_runtime

    def switch_profile(self, profile: str) -> None:
        if profile not in PROFILES:
            raise ValueError("Unknown task profile: " + profile)
        self.state["profile"] = profile
        self.store.commit()

    def register_user_urls(self, text: str) -> None:
        for raw in URL_PATTERN.findall(text):
            try:
                self._register_candidate(raw.rstrip(".,;!?，。；！？"), source_type="user_url")
            except Exception:
                continue
        self.store.commit()

    def before_execute(self, call: dict) -> dict | None:
        name = call.get("name")
        try:
            args = json.loads(call.get("arguments", ""))
        except Exception:
            return None
        intent = _web_intent(name, args)
        if intent and self.state.get("constraints", {}).get("network_forbidden"):
            return error_result(call, "network_forbidden", "用户要求禁止联网；本次未发起网络请求。请使用已授权的本地资料。")
        if intent and self.state["profile"] == "direct_answer":
            self.switch_profile("web_quick")
        if intent:
            routes = self.state.setdefault("capability_routes", [])
            if "web" not in routes:
                routes.append("web")
        # 缓存读取和无效 URL 不消耗实际网络预算。
        if intent == "fetch" and name == "fetch_web_content":
            checked = self._check_fetch(call, args["url"])
            if checked is not None:
                return checked
        return None

    def after_execute(self, call: dict, result: dict) -> dict:
        if result.get("pending"):
            return result
        data = result.get('data') or {}
        if data.get('kind') == 'builtin_tool' and data.get('status') in {'queued', 'running', 'stopping', 'unknown'}:
            return result
        if call.get('name') == 'read_job' and data.get('kind') == 'builtin_tool' and data.get('origin_call'):
            original = data['origin_call']
            if original.get('name') not in FETCH_TOOLS:
                return result
            identity = data['job_id']
            completed = self.state.setdefault('research_jobs', [])
            if identity in completed:
                return result
            payload = data.get('result') or {}
            error = data.get('operation_error')
            if error is None and data.get('status') in {'failed', 'stopped', 'cancelled'}:
                error = {'code': 'job_' + data['status'], 'message': '网页作业未完成', 'retryable': False}
            completed.append(identity)
            try:
                self.after_execute(original, {'call_id': original['id'], 'ok': error is None, 'data': payload,
                                              'error': error, 'source_refs': payload.get('source_refs', []), 'artifact_refs': []})
            except BaseException:
                completed.remove(identity)
                raise
            return result
        call_id = call.get("id")
        cached = bool((result.get("data") or {}).get("cached"))
        ledger = self.store.record.calls.get(call_id, {})
        executed = ledger.get('executed', False)
        name = call.get("name")
        try:
            args = json.loads(call.get("arguments", ""))
        except Exception:
            args = {}
        intent = _web_intent(name, args)
        if intent:
            completed = self.state.setdefault('research_completed_calls', [])
            if call_id in completed:
                return result
            completed.append(call_id)
        if call_id not in self.state["counted_calls"]:
            self.state["counted_calls"].append(call_id)
            counter = {"query": "search_queries", "fetch": "search_fetches"}.get(intent)
            if counter and not cached and executed:
                self.state["usage"][counter] += 1
            if cached:
                self.state["usage"]["cache_hits"] = self.state["usage"].get("cache_hits", 0) + 1
        self.state["last_tool_name"] = name
        if intent == "query":
            self._after_query(call, result)
        elif intent == "fetch":
            self._after_fetch(call, result)
        elif result.get("ok") and str(name).startswith("mcp_"):
            before = self.state["candidate_count"]
            for link in (result.get("data") or {}).get("candidate_links", ()):
                try:
                    self._register_candidate(link["url"], source_type="mcp", title=link["title"],
                                             query_call_id=call_id, tool_name=name)
                except (ValueError, SearchError):
                    continue
            if self.state["candidate_count"] > before and self.state["profile"] == "direct_answer":
                self.switch_profile("web_quick")
        self.store.commit()
        return result

    def note_model(self, tokens: int) -> None:
        self.state["usage"]["model_calls"] += 1
        self.state["usage"]["total_tokens"] += max(tokens, 0)
        self.store.commit()

    def should_force_answer(self) -> bool:
        # 相关性和字段覆盖属于当前问题，而非品牌。
        return self.state["phase"] == "answer" or bool(self.state.get("stop_reason"))

    def force_answer(self, reason: str) -> None:
        if self.state.get("phase") not in {"recover", "done"}:
            self.state["phase"] = "answer"
        self.state["stop_reason"] = self.state.get("stop_reason") or reason

    def _source_unavailable(self, reason: str) -> None:
        self.state.setdefault("capability_blocks", {})["web"] = reason

    def _check_fetch(self, call: dict, raw_url: str) -> dict | None:
        args = json.loads(call['arguments'])
        if args.get('query'):
            return error_result(call, 'validation_error', 'query 与 url 必须且只能指定一个')
        try:
            url = canonicalize_url(raw_url)
        except Exception:
            return error_result(call, "unsafe_url", "URL 不是允许的 HTTP(S) 公网地址")
        candidate = self.state["candidates"].get(url)
        if candidate is None:
            try:
                self._register_candidate(url, source_type="tool_request")
            except ValueError:
                return error_result(call, "unsafe_url", "URL不属于允许访问的网络地址")
            candidate = self.state["candidates"][url]
        failure = self.state['failed_resources'].get(url)
        permanent = failure and not failure.get('retryable') and (failure.get('http_status') or failure.get('code') in NON_RETRYABLE)
        if permanent and not args.get('refresh'):
            return error_result(call, failure['code'], '此资源已确认不可重试；如外部状态已变化，可显式 refresh 重新请求。' +
                                (f" HTTP {failure['http_status']}" if failure.get('http_status') else ''))
        if not args.get('refresh'):
            source = next(((ref, item) for ref, item in reversed(list(self.store.record.sources.items()))
                           if item.get('kind') == 'web' and url in {item.get('requested_url'), item.get('url')}), None)
            if source:
                ref, _ = source
                page_path = self.store.root / 'sources' / (ref + '.json')
                page = json.loads(page_path.read_text(encoding='utf-8'))
                try:
                    data = page_source(page, ref, args, cached=True)
                except ToolError as exc:
                    return error_result(call, exc.error_type, str(exc))
                return {
                    "call_id": call.get("id", ""), "ok": True,
                    "data": {**data, 'duplicate': True},
                    "error": None, "source_refs": [ref],
                    "artifact_refs": [], "truncated": data['truncated'],
                }
        return None

    def _after_query(self, call: dict, result: dict) -> None:
        try:
            args = json.loads(call["arguments"])
            query = normalized_text(args["query"]).casefold()
        except Exception:
            query = ""
        if not result.get("ok"):
            ledger = self.store.record.calls.get(call.get('id'), {})
            if query and ledger.get('executed') and query not in self.state["completed_queries"]:
                self.state["completed_queries"].append(query)
            diagnostics = (result.get('data') or {}).get('diagnostics') or self.state.get('search_diagnostics') or {}
            if diagnostics:
                self.state['search_diagnostics'] = diagnostics
                self.store.event('search_diagnostics', provider='none', new_candidates=0,
                                 repeated_candidates=False, provider_errors=diagnostics.get('fallback_errors', []))
            if diagnostics.get('all_providers_failed') and self._all_candidates_exhausted():
                self._source_unavailable('search_service_unavailable')
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
                    self._source_unavailable('external_source_unavailable')
            self.store.event('search_diagnostics', provider=diagnostics['provider'], new_candidates=added,
                             repeated_candidates=diagnostics['repeated_candidates'],
                             provider_errors=diagnostics['fallback_errors'])

    def _after_fetch(self, call: dict, result: dict) -> None:
        data = result.get('data') or {}
        if result.get('ok') and isinstance(data.get('text'), str) and data['text'].strip():
            span = hashlib.sha256((str(data.get('url')) + '\n' + data['text']).encode('utf-8')).hexdigest()
            spans = self.state.setdefault('observed_spans', [])
            if span not in spans:
                spans.append(span)
                self.state['usage']['observations'] += 1
        try:
            canonicalize = fetch_canonicalizer(call.get("name"))
            requested = canonicalize(json.loads(call["arguments"])["url"])
        except Exception:
            return
        boundary = 'private' if call.get('name') == 'fetch_private_resource' else 'public'
        if result.get('ok'):
            self._register_candidate(requested, source_type='tool_request', network_boundary=boundary)
        candidate = self.state["candidates"].get(requested)
        if result.get('ok'):
            self.state['failed_resources'].pop(requested, None)
            args = json.loads(call['arguments'])
            depth = (candidate or {}).get('depth', 0)
            ceiling = args.get('discovery_depth')
            refs = result.get('source_refs') or data.get('source_refs') or []
            if ceiling is None or depth < ceiling:
                for link in data.get('links', []):
                    try:
                        self._register_candidate(link['url'], source_type='page_link', title=str(link.get('title', '')),
                                                 parent_source_ref=refs[0] if refs else None,
                                                 parent_url=data.get('url', requested), depth=depth + 1, network_boundary=boundary)
                    except (KeyError, TypeError, ValueError, RuntimeError):
                        continue
            if data.get('cached') and (not data.get('text', '').strip() or all(ref in self.state['evidence_refs'] for ref in refs)):
                return
        if not result.get("ok"):
            # 预检拒绝没有触及网络，
            # 不能覆盖上次实际执行的失败记录。
            if not self.store.record.calls.get(call.get('id'), {}).get('executed', False):
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
                candidate["status"] = "retryable" if retryable else "failed"
            if self._all_candidates_exhausted():
                self._source_unavailable('external_source_unavailable')
            return
        data = result.get("data") or {}
        refs = result.get("source_refs") or data.get("source_refs") or []
        if not refs or not isinstance(data.get("text"), str):
            return
        if not data['text'].strip():
            if candidate:
                candidate.update(status='downloaded', source_ref=refs[0])
            return
        final_url = canonicalize(data.get("url") or requested)
        if final_url != requested and final_url not in self.state["candidates"]:
            self.state["candidates"][final_url] = {**(candidate or {}), "canonical_url": final_url, "source_type": "redirect"}
        source = self.store.record.sources.get(refs[0], {})
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
            if status in {'candidate', 'downloaded'}:
                return False
            if status == 'retryable':
                failure = self.state.get('failed_resources', {}).get(candidate.get('canonical_url'), {})
                if failure.get('retryable'):
                    return False
        return True

    def _register_candidate(self, raw_url: str, *, source_type: str, network_boundary='public', **metadata) -> str:
        # Discovery is a reference inventory, never permission to fetch.
        try:
            url = fetch_canonicalizer('fetch_private_resource' if network_boundary == 'private' else 'fetch_web_content')(raw_url)
        except SearchError:
            url = fetch_canonicalizer('fetch_private_resource')(raw_url)
            network_boundary = 'private'
        host = urlsplit(url).hostname or ""
        try:
            if not ipaddress.ip_address(host).is_global:
                network_boundary = "private"
        except ValueError:
            if host == "localhost" or host.endswith(".localhost"):
                network_boundary = "private"
        if url not in self.state["candidates"]:
            self.state["candidates"][url] = {
                "canonical_url": url, "domain": host.lower(), "source_type": source_type,
                "status": "candidate", "network_boundary": network_boundary, **metadata,
            }
            self.state["candidate_count"] = len(self.state["candidates"])
        return url
