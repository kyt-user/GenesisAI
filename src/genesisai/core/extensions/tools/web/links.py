"""从已抓取 HTML 文档中有界发现同源链接。"""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from genesisai.core.extensions.tools.web.contracts import canonicalize_url


class _Links(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = canonicalize_url(base)
        self.origin = urlsplit(self.base)[:2]
        self.main_depth = 0
        self.anchor = None
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == 'main':
            self.main_depth += 1
        if tag != 'a':
            return
        self.anchor = None
        attrs = dict(attrs)
        href = attrs.get('href', '')
        if not href or href.startswith('#') or len(self.links) >= 200:
            return
        try:
            url = canonicalize_url(urljoin(self.base, href))
            if urlsplit(url)[:2] != self.origin or url == self.base or len(url) > 600:
                return
        except (ValueError, RuntimeError):
            return
        self.anchor = {'url': url, 'title': (attrs.get('aria-label') or '')[:100], 'main': bool(self.main_depth)}

    def handle_data(self, data):
        if self.anchor:
            self.anchor['title'] = (self.anchor['title'] + ' ' + data.strip()).strip()[:100]

    def handle_endtag(self, tag):
        if tag == 'main':
            self.main_depth = max(0, self.main_depth - 1)
        if tag == 'a' and self.anchor:
            old = self.links.get(self.anchor['url'])
            if old is None or self.anchor['main']:
                self.links[self.anchor['url']] = self.anchor
            self.anchor = None


def discover_links(html, base):
    parser = _Links(base)
    parser.feed(html)
    result, size = [], 0
    for link in sorted(parser.links.values(), key=lambda item: not item['main']):
        cost = len(link['url']) + len(link['title']) + 40
        if size + cost > 6000 or len(result) >= 40:
            break
        result.append({'url': link['url'], 'title': link['title'] or link['url']})
        size += cost
    return result

