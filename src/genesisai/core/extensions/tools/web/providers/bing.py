"""公开的 Bing RSS 后备搜索，不使用账户或 API 凭据。"""
import xml.etree.ElementTree as ET

from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, classify_transport_error


class BingSearchProvider:
    name = 'bing_rss'

    def __init__(self, client):
        self.client = client

    def search(self, query, limit=5):
        try:
            response = self.client.get('https://www.bing.com/search', params={'q':query.query,'format':'rss'})
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
        for item in root.findall('./channel/item')[:limit]:
            try:
                results.append(SearchHit(provider=self.name,rank=len(results)+1,title=item.findtext('title',''),url=item.findtext('link',''),snippet=item.findtext('description','')[:1000]))
            except SearchError:
                continue
        return results

