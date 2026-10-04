"""无需密钥的 DuckDuckGo HTML 搜索提供商。"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, SearchQuery, classify_transport_error
from genesisai.core.extensions.tools.web.providers.base import SearchProvider


class _ResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", ""):
            self._href = attrs.get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            href = self._href
            parsed = urlsplit(href)
            if parsed.netloc.endswith("duckduckgo.com"):
                href = parse_qs(parsed.query).get("uddg", [href])[0]
            self.results.append(("".join(self._parts).strip(), href))
            self._href = None


class DuckDuckGoSearchProvider(SearchProvider):
    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    def __init__(self, *, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=10.0,
            follow_redirects=False, trust_env=False,
            headers={"User-Agent": "GenesisAI/0.1 public-research-agent"},
        )

    def search(self, query: SearchQuery, limit: int = 10) -> list[SearchHit]:
        try:
            response = self.client.get(self.endpoint, params={"q": query.query})
        except Exception as exc:
            failure = classify_transport_error(exc)
            if failure is None:
                raise
            code, reason, retryable = failure
            raise SearchError(code, "DuckDuckGo request failed", retryable=retryable, reason=reason) from exc
        if response.status_code == 429:
            raise SearchError("provider_rate_limited", "DuckDuckGo rate limit reached", retryable=True, reason="rate_limit")
        if response.status_code in {401, 403}:
            raise SearchError("access_denied", "DuckDuckGo denied automated access", reason="access_denied")
        if response.status_code != 200:
            raise SearchError("provider_unavailable", "DuckDuckGo returned a non-success status", retryable=response.status_code >= 500, reason="http_status")
        parser = _ResultParser()
        try:
            parser.feed(response.text)
        except Exception as exc:
            raise SearchError("provider_invalid_response", "DuckDuckGo returned invalid HTML", reason="parse_error") from exc
        hits = []
        for rank, (title, url) in enumerate(parser.results[:limit], 1):
            try:
                hits.append(SearchHit(self.name, rank, title, url))
            except SearchError:
                continue
        return hits

