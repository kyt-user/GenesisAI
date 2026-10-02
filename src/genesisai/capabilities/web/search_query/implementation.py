"""查询公开网络并返回必须继续抓取候选结果。"""

import json

from genesisai.shared.security import ToolError
from genesisai.capabilities.web.contracts import SAFE_PROVIDER_ERROR_CODES, SAFE_PROVIDER_REASONS, SearchQuery


class Implementation:
    def prepare(self, context, args):
        return {
            "name": "search_query",
            "args": args,
            "target": "公开网络请求：" + args["query"],
            "grants": context.store.data["grants"],
            "output": context.store.data["output"],
        }

    def execute(self, context, args):
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
                reason = getattr(exc, 'reason', None)
                if reason in SAFE_PROVIDER_REASONS:
                    failure['reason'] = reason
                errors.append(failure)
        diagnostics = {
            "provider": None,
            "fallback_errors": errors,
            "new_candidates": 0,
            "repeated_candidates": False,
            "all_providers_failed": True,
        }
        context.store.data["run_runtime"]["search_diagnostics"] = diagnostics
        context.store.event(
            "search_diagnostics", provider="none", new_candidates=0,
            repeated_candidates=False, provider_errors=errors,
        )
        context.store.save()
        raise ToolError(
            "search_unavailable",
            "搜索不可用或没有结果：" + json.dumps(errors, ensure_ascii=False),
            True,
        )

