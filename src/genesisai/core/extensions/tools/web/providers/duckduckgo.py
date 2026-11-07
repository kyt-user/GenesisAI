"""DuckDuckGo HTML search; continuation is read from the actual next-page form."""
from __future__ import annotations
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.contracts import SearchError, SearchHit, SearchPage, SearchQuery, classify_transport_error
from genesisai.core.extensions.tools.web.providers.base import SearchProvider, bounded_search


class _ResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results, self.forms = [], []
        self.href, self.parts, self.form = None, [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.form = {'fields': {}, 'next': False}
        if tag == 'input' and self.form is not None:
            if attrs.get('name'):
                self.form['fields'][attrs['name']] = attrs.get('value', '')
            if attrs.get('type') == 'submit' and 'next' in attrs.get('value', '').lower():
                self.form['next'] = True
        if tag == 'a' and 'result__a' in attrs.get('class', ''):
            self.href, self.parts = attrs.get('href'), []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)
        if self.form is not None and data.strip().lower() == 'next':
            self.form['next'] = True

    def handle_endtag(self, tag):
        if tag == 'form' and self.form is not None:
            if self.form['next'] and self.form['fields']:
                self.forms.append(self.form['fields'])
            self.form = None
        if tag == 'a' and self.href:
            parsed = urlsplit(self.href)
            host = parsed.hostname or ''
            href = parse_qs(parsed.query).get('uddg', [self.href])[0] if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com') else self.href
            self.results.append((''.join(self.parts).strip(), href))
            self.href = None


class DuckDuckGoSearchProvider(SearchProvider):
    name = 'duckduckgo'
    endpoint = 'https://html.duckduckgo.com/html/'

    def __init__(self, *, client=None):
        self.client = client or Network()

    @bounded_search
    def search(self, query: SearchQuery, limit=10):
        params = {'q': query.terms()}
        if query.country or query.language:
            params['kl'] = '-'.join(value for value in (query.country, query.language) if value)
        if query.freshness:
            params['df'] = query.freshness
        hits, position, visited = [], 0, set()
        while True:
            marker = tuple(sorted(params.items()))
            if marker in visited:
                raise SearchError('provider_invalid_response', 'DuckDuckGo repeated its pagination cursor', reason='invalid_response')
            visited.add(marker)
            try:
                response = self.client.get(self.endpoint, params=params)
            except Exception as exc:
                failure = classify_transport_error(exc)
                if failure is None:
                    raise
                code, reason, retryable = failure
                raise SearchError(code, 'DuckDuckGo request failed', retryable=retryable, reason=reason) from exc
            if response.status_code == 429:
                raise SearchError('provider_rate_limited', 'DuckDuckGo rate limit reached', retryable=True, reason='rate_limit')
            if response.status_code in {401, 403}:
                raise SearchError('access_denied', 'DuckDuckGo denied automated access', reason='access_denied')
            if response.status_code != 200:
                raise SearchError('provider_unavailable', 'DuckDuckGo returned a non-success status', retryable=response.status_code in {408, 425, 500, 502, 503, 504}, reason='http_status')
            parser = _ResultParser()
            try:
                parser.feed(response.text); parser.close()
            except Exception as exc:
                raise SearchError('provider_invalid_response', 'DuckDuckGo returned invalid HTML', reason='parse_error') from exc
            for index, (title, url) in enumerate(parser.results):
                position += 1
                if position <= query.offset:
                    continue
                try:
                    hits.append(SearchHit(self.name, position, title, url))
                except SearchError:
                    continue
                if len(hits) >= limit:
                    more = index + 1 < len(parser.results) or bool(parser.forms)
                    return SearchPage(hits, position if more else None, 'html_next_form')
            if not parser.forms:
                return SearchPage(hits, None, 'html_next_form')
            params = parser.forms[0]
