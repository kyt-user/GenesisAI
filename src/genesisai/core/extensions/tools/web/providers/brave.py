"""Brave Search API pages, bounded by the shared public-network budget."""
from __future__ import annotations
import os

from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, SearchPage, SearchQuery, classify_transport_error
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.providers.base import SearchProvider, bounded_search


class BraveSearchProvider(SearchProvider):
    name = 'brave'
    endpoint = 'https://api.search.brave.com/res/v1/web/search'
    max_results = 200

    def __init__(self, api_key=None, *, client=None):
        self.api_key = api_key if api_key is not None else os.getenv('BRAVE_SEARCH_API_KEY', '')
        self.client = client or Network()

    @bounded_search
    def search(self, query: SearchQuery, limit=10):
        if not self.api_key:
            raise SearchError('provider_auth_error', 'Brave Search API key is not configured', reason='missing_credentials')
        hits = []
        for page in range(query.offset // 20, 10):
            params = {'q': query.terms(), 'count': 20, 'offset': page}
            if query.country:
                params['country'] = query.country
            if query.language:
                params['search_lang'] = query.language
            if query.freshness:
                params['freshness'] = query.freshness
            try:
                response = self.client.get(self.endpoint, params=params,
                    headers={'Accept': 'application/json', 'X-Subscription-Token': self.api_key})
            except Exception as exc:
                failure = classify_transport_error(exc)
                if failure is None:
                    raise
                code, reason, retryable = failure
                raise SearchError(code, 'Brave Search request failed', retryable=retryable, reason=reason) from exc
            if response.status_code in {401, 403}:
                raise SearchError('provider_auth_error', 'Brave Search rejected authentication', reason='authentication_rejected')
            if response.status_code == 429:
                raise SearchError('provider_rate_limited', 'Brave Search rate limit reached', retryable=True, reason='rate_limit')
            if response.status_code != 200:
                raise SearchError('provider_unavailable', 'Brave Search returned HTTP ' + str(response.status_code),
                                  retryable=response.status_code in {408, 425, 500, 502, 503, 504}, reason='http_status')
            try:
                payload = response.json()
                items = payload.get('web', {}).get('results', [])
                if not isinstance(items, list):
                    raise ValueError()
            except (ValueError, AttributeError) as exc:
                raise SearchError('provider_invalid_response', 'Brave Search returned invalid JSON', reason='parse_error') from exc
            for index, item in enumerate(items):
                rank = page * 20 + index + 1
                if rank <= query.offset or not isinstance(item, dict) or not item.get('url'):
                    continue
                try:
                    hits.append(SearchHit(self.name, rank, str(item.get('title', '')), str(item['url']),
                                          str(item.get('description', '')), item.get('age')))
                except SearchError:
                    continue
                if len(hits) >= limit:
                    more = index + 1 < len(items) or payload.get('query', {}).get('more_results_available', False)
                    return SearchPage(hits, rank if more and rank < self.max_results else None, 'brave_web_max_200')
            if not payload.get('query', {}).get('more_results_available', False):
                break
        return SearchPage(hits, None, 'brave_web_max_200')
