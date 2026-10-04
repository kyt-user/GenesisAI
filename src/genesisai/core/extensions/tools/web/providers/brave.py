"""Brave Search API 搜索提供商。"""

from __future__ import annotations

import os
import time

import httpx

from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, SearchQuery, classify_transport_error
from genesisai.core.extensions.tools.web.providers.base import SearchProvider


class BraveSearchProvider(SearchProvider):
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, *, client: httpx.Client | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("BRAVE_SEARCH_API_KEY", "")
        self.client = client or httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False)

    def search(self, query: SearchQuery, limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise SearchError("provider_auth_error", "Brave Search API key is not configured", reason="missing_credentials")
        params = {"q": query.query, "count": min(max(limit, 1), 20)}
        if query.country:
            params["country"] = query.country
        if query.language:
            params["search_lang"] = query.language
        response = None
        transport_failure = None
        for attempt in range(3):
            try:
                response = self.client.get(
                    self.endpoint,
                    params=params,
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                )
            except Exception as exc:
                transport_failure = classify_transport_error(exc)
                if transport_failure is None:
                    raise
                if attempt == 2:
                    code, reason, retryable = transport_failure
                    raise SearchError(
                        code, "Brave Search request failed", retryable=retryable, reason=reason,
                    ) from exc
                time.sleep(0.05 * (2 ** attempt))
                continue
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempt == 2:
                break
            retry_after = response.headers.get("retry-after")
            try:
                delay = min(max(float(retry_after), 0), 2) if retry_after else 0.05 * (2 ** attempt)
            except ValueError:
                delay = 0.05 * (2 ** attempt)
            time.sleep(delay)
        if response is None:
            code, reason, retryable = transport_failure or ("provider_unavailable", "connection_error", True)
            raise SearchError(code, "Brave Search request failed", retryable=retryable, reason=reason)
        if response.status_code in {401, 403}:
            raise SearchError("provider_auth_error", "Brave Search rejected authentication", reason="authentication_rejected")
        if response.status_code == 429:
            raise SearchError("provider_rate_limited", "Brave Search rate limit reached", retryable=True, reason="rate_limit")
        if response.status_code >= 500:
            raise SearchError("provider_unavailable", "Brave Search is unavailable", retryable=True, reason="http_status")
        if response.status_code != 200:
            raise SearchError("provider_invalid_response", "Brave Search returned a non-success status", reason="http_status")
        try:
            items = response.json().get("web", {}).get("results", [])
        except (ValueError, AttributeError) as exc:
            raise SearchError("provider_invalid_response", "Brave Search returned invalid JSON", reason="parse_error") from exc
        if not isinstance(items, list):
            raise SearchError("provider_invalid_response", "Brave Search results are invalid", reason="invalid_response")
        hits = []
        for rank, item in enumerate(items[:limit], 1):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            try:
                hits.append(SearchHit(
                    provider=self.name,
                    rank=rank,
                    title=str(item.get("title", "")),
                    url=str(item["url"]),
                    snippet=str(item.get("description", ""))[:1000],
                    published_at=item.get("age"),
                ))
            except SearchError:
                continue
        return hits

