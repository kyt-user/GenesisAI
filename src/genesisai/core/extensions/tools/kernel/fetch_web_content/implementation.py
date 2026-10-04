"""内核工具：网络检索与正文抓取。吸收原 search_query + search_fetch。"""

import json
import time

from genesisai.shared.security import ToolError, digest
from genesisai.core.state.store import atomic_json
from genesisai.core.extensions.tools.web.contracts import SAFE_PROVIDER_ERROR_CODES, SAFE_PROVIDER_REASONS, SearchQuery


class Implementation:
    def prepare(self, context, args):
        if args.get("url"):
            target = "公开网络请求：" + args["url"]
        elif args.get("query"):
            target = "公开网络请求：" + args["query"]
        else:
            raise ToolError("validation_error", "必须提供 query 或 url")
        return {"name": "fetch_web_content", "args": args, "target": target,
                "grants": context.store.data["grants"], "output": context.store.data["output"]}

    def execute(self, context, args):
        if args.get("url"):
            return self._fetch(context, args)
        if args.get("query"):
            return self._search(context, args)
        raise ToolError("validation_error", "必须提供 query 或 url")

    def _search(self, context, args):
        errors = []
        for provider in context.providers:
            try:
                hits = provider.search(SearchQuery(args["query"]), limit=5)
                if hits:
                    return {
                        "hits": [hit.to_dict() if hasattr(hit, "to_dict") else hit for hit in hits[:5]],
                        "provider": provider.name,
                        "fallback_errors": errors,
                    }
                errors.append({"provider": provider.name, "code": "empty_results"})
            except Exception as exc:
                raw_code = getattr(exc, "error_type", "provider_unavailable")
                failure = {
                    "provider": getattr(provider, "name", "unknown"),
                    "code": raw_code if raw_code in SAFE_PROVIDER_ERROR_CODES else "provider_unavailable",
                    "retryable": bool(getattr(exc, "retryable", False)),
                }
                reason = getattr(exc, "reason", None)
                if reason in SAFE_PROVIDER_REASONS:
                    failure["reason"] = reason
                errors.append(failure)
        diagnostics = {
            "provider": None, "fallback_errors": errors, "new_candidates": 0,
            "repeated_candidates": False, "all_providers_failed": True,
        }
        context.store.data["run_runtime"]["search_diagnostics"] = diagnostics
        context.store.event("search_diagnostics", provider="none", new_candidates=0,
                            repeated_candidates=False, provider_errors=errors)
        context.store.save()
        raise ToolError("search_unavailable", "搜索不可用或没有结果：" + json.dumps(errors, ensure_ascii=False), True)

    def _fetch(self, context, args):
        cached = next(
            ((ref, value) for ref, value in context.store.data["sources"].items()
             if value.get("requested_url") == args["url"]),
            None,
        )
        if cached:
            ref, _ = cached
            page = json.loads((context.store.root / "sources" / f"{ref}.json").read_text(encoding="utf-8"))
        else:
            page = context.network.fetch(args["url"])
            ref = context.store.source(
                kind="web", url=page["url"], requested_url=args["url"], title=page["title"],
                fetched_at=time.time(), sha256=digest(page["text"]),
            )
            atomic_json(context.store.root / "sources" / f"{ref}.json", page)
        offset, limit = args.get("offset", 0), args.get("limit", 4000)
        text = page["text"]
        return {
            "text": text[offset: offset + limit],
            "url": page["url"],
            "title": page["title"],
            "source_refs": [ref],
            "truncated": page["truncated"] or len(text) > offset + limit,
            "next_offset": offset + limit if len(text) > offset + limit else None,
            "cached": bool(cached),
            "links": page.get("links", []),
            "query": None,
        }