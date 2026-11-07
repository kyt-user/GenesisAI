"""内核工具：网络检索与正文抓取。吸收原 search_query + search_fetch。"""

import json
import time
import hashlib
from dataclasses import asdict, replace
from urllib.parse import urlsplit

from genesisai.shared.security import ToolError, digest
from genesisai.core.persistence.files import atomic_json
from genesisai.core.extensions.tools.web.contracts import SAFE_PROVIDER_ERROR_CODES, SAFE_PROVIDER_REASONS, SearchQuery
from genesisai.core.extensions.tools.web.budget import NetworkLimits, active_limits, network_budget, remaining
from genesisai.core.extensions.tools.web.source_pages import page_source
from genesisai.shared.web import canonicalize_url
from genesisai.core.tools.base import PartialToolResult


class Implementation:
    name = 'fetch_web_content'
    source_kind = 'web'
    canonicalize = staticmethod(canonicalize_url)

    def prepare(self, context, args):
        if bool(args.get('query')) == bool(args.get('url')):
            raise ToolError('validation_error', 'query 与 url 必须且只能指定一个')
        NetworkLimits.parse(args.get('network_limits'))
        if args.get("url"):
            target = "公开网络请求：" + args["url"]
        elif args.get("query"):
            target = "公开网络请求：" + args["query"]
        else:
            raise ToolError("validation_error", "必须提供 query 或 url")
        return {'name': self.name, 'args': args, 'target': target, 'grants': list(context.store.record.grants), 'output': context.store.record.output}

    def execute(self, context, args):
        limits = NetworkLimits.parse(args.get('network_limits'))
        token = active_limits.set(limits)
        try:
            with network_budget(limits):
                return self._fetch(context, args) if args.get('url') else self._search(context, args)
        finally:
            active_limits.reset(token)

    def _search(self, context, args):
        errors = []
        query = SearchQuery(args['query'], offset=args.get('offset', 0), language=args.get('language'),
                            country=args.get('country'), freshness=args.get('freshness'), allowed_domains=tuple(args.get('allowed_domains', [])))
        count = args.get('result_count', 10)
        for provider in context.providers:
            if args.get('provider') and provider.name != args['provider']:
                continue
            remaining()
            try:
                query_limits = {**asdict(active_limits.get()), 'total_seconds': remaining()}
                batch = provider.search(replace(query, network_limits=query_limits), limit=count)
                hits = batch.hits
                values = [hit.to_dict() if hasattr(hit, 'to_dict') else hit for hit in hits]
                if query.allowed_domains:
                    values = [hit for hit in values if any((urlsplit(hit['url']).hostname or '').lower() == domain
                              or (urlsplit(hit['url']).hostname or '').lower().endswith('.' + domain) for domain in query.allowed_domains)]
                if hits:
                    return {
                        "hits": values[:count], 'offset': query.offset, 'result_count': count,
                        'next_offset': batch.next_offset, 'coverage': batch.coverage,
                        "provider": provider.name,
                        "fallback_errors": errors,
                        'provider_result_limit': getattr(provider, 'max_results', None),
                    }
                if query.offset and args.get('provider'):
                    return {'hits': [], 'offset': query.offset, 'next_offset': batch.next_offset, 'provider': provider.name,
                            'fallback_errors': errors, 'coverage': batch.coverage}
                errors.append({"provider": provider.name, "code": "empty_results"})
            except Exception as exc:
                if getattr(exc, 'error_type', None) in {'user_cancelled', 'timeout'}:
                    raise
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
        result = PartialToolResult('search_unavailable', '搜索不可用或没有结果：' + json.dumps(errors, ensure_ascii=False),
                                   diagnostics=diagnostics)
        result.error['retryable'] = any(error.get('retryable') for error in errors)
        return result

    def cache_key(self):
        return None

    def _fetch(self, context, args):
        requested = self.canonicalize(args['url'])
        cached = None if args.get('refresh') else next(((ref, value) for ref, value in reversed(list(context.store.record.sources.items()))
                                                      if value.get('kind') == self.source_kind and value.get('requested_url') == requested
                                                      and value.get('access_digest') == self.cache_key()), None)
        if cached:
            ref, _ = cached
            page = json.loads((context.store.root / "sources" / f"{ref}.json").read_text(encoding="utf-8"))
        else:
            page = self.fetch_source(context, requested, args)
            raw = page.pop('raw_content', None)
            ref = context.store.source(
                kind=self.source_kind, url=page["url"], requested_url=requested, title=page["title"],
                fetched_at=time.time(), sha256=digest(page["text"]), access_digest=self.cache_key(),
            )
            if raw is not None:
                raw_path = context.store.root / 'sources' / (ref + '.bin')
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(raw)
                page['raw_source'] = {'source_ref': ref, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                                      'media_type': page.get('media_type')}
            atomic_json(context.store.root / "sources" / f"{ref}.json", page)
        return page_source(page, ref, args, cached=bool(cached))

    def fetch_source(self, context, url, args):
        return (context.network.fetch(url, limits={**asdict(active_limits.get()), 'total_seconds': remaining()})
                if args.get('network_limits') else context.network.fetch(url))
