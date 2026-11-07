"""公开的 Bing RSS 后备搜索，不使用账户或 API 凭据。"""
import xml.etree.ElementTree as ET

from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, SearchPage, classify_transport_error
from genesisai.core.extensions.tools.web.providers.base import bounded_search


class BingSearchProvider:
    name = 'bing_rss'

    def __init__(self, client):
        self.client = client

    @bounded_search
    def search(self, query, limit=10):
        try:
            params = {'q': query.terms(), 'format': 'rss'}
            if query.language:
                params['setlang'] = query.language
            if query.country:
                params['cc'] = query.country
            if query.freshness:
                raise SearchError('provider_invalid_response', 'Bing RSS does not expose a verified freshness filter', reason='invalid_response')
            response = self.client.get('https://www.bing.com/search', params=params)
        except Exception as exc:
            failure = classify_transport_error(exc)
            if failure is None:
                raise
            code, reason, retryable = failure
            raise SearchError(code, 'Bing request failed', retryable=retryable, reason=reason) from exc
        if response.status_code == 429:
            raise SearchError('provider_rate_limited', 'Bing rate limit reached', retryable=True, reason='rate_limit')
        if response.status_code in {401, 403}:
            raise SearchError('access_denied', 'Bing denied automated access', reason='access_denied')
        if response.status_code != 200:
            raise SearchError('provider_unavailable', 'Bing returned a non-success status', retryable=response.status_code >= 500, reason='http_status')
        if b'<!DOCTYPE' in response.content.upper() or b'<!ENTITY' in response.content.upper():
            raise SearchError('provider_invalid_response','Unsupported XML declaration', reason='invalid_response')
        try:
            root=ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise SearchError('provider_invalid_response','Bing did not return RSS', reason='parse_error') from exc
        results=[]
        for item in root.findall('./channel/item'):
            try:
                results.append(SearchHit(provider=self.name,rank=len(results)+1,title=item.findtext('title',''),url=item.findtext('link',''),snippet=item.findtext('description','')))
            except SearchError:
                continue
        end = query.offset + limit
        return SearchPage(results[query.offset:end], end if end < len(results) else None, 'complete_rss_response_only')

