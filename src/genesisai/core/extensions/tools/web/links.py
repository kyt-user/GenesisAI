"""完整发现 HTTP(S) 链接；发现本身不授予访问权限。"""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from genesisai.core.extensions.tools.web.contracts import canonicalize_url


class _Links(HTMLParser):
    def __init__(self, base, canonicalize):
        super().__init__(convert_charrefs=True)
        self.canonicalize = canonicalize
        self.base = canonicalize(base)
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
        if not href or href.startswith('#'):
            return
        try:
            url = self.canonicalize(urljoin(self.base, href))
            if url == self.base:
                return
        except (ValueError, RuntimeError):
            return
        self.anchor = {'url': url, 'title': attrs.get('aria-label') or '', 'main': bool(self.main_depth),
                       'cross_domain': urlsplit(url)[:2] != self.origin}

    def handle_data(self, data):
        if self.anchor:
            self.anchor['title'] = (self.anchor['title'] + ' ' + data.strip()).strip()

    def handle_endtag(self, tag):
        if tag == 'main':
            self.main_depth = max(0, self.main_depth - 1)
        if tag == 'a' and self.anchor:
            old = self.links.get(self.anchor['url'])
            if old is None or self.anchor['main']:
                self.links[self.anchor['url']] = self.anchor
            self.anchor = None


def discover_links(html, base, *, canonicalize=canonicalize_url):
    parser = _Links(base, canonicalize)
    parser.feed(html)
    parser.close()
    return [{'url': link['url'], 'title': link['title'] or link['url'], 'cross_domain': link['cross_domain']}
            for link in sorted(parser.links.values(), key=lambda item: not item['main'])]

