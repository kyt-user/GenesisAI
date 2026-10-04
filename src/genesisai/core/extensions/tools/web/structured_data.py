"""从标准 JSON-LD 和产品 meta 标签中提取有界产品事实。"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from genesisai.core.extensions.tools.web.contracts import normalized_text


MAX_HTML_CHARS = 1_500_000
MAX_JSON_LD_CHARS = 250_000
MAX_FACTS = 20
_PRICE = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,4})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class _StructuredDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.meta: dict[str, str] = {}
        self._json_ld = False
        self._parts: list[str] = []
        self._script_chars = 0

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == 'script' and str(values.get('type', '')).lower().split(';', 1)[0].strip() == 'application/ld+json':
            self._json_ld = True
            self._parts = []
            self._script_chars = 0
        elif tag.lower() == 'meta':
            name = str(values.get('property') or values.get('name') or '').lower()
            if name in {'product:price:amount', 'product:price:currency', 'og:title'}:
                self.meta[name] = normalized_text(str(values.get('content') or ''))[:200]

    def handle_data(self, data):
        if self._json_ld and self._script_chars < MAX_JSON_LD_CHARS:
            remaining = MAX_JSON_LD_CHARS - self._script_chars
            self._parts.append(data[:remaining])
            self._script_chars += min(len(data), remaining)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self._json_ld:
            self.scripts.append(''.join(self._parts))
            self._json_ld = False
            self._parts = []


def _nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _types(node):
    value = node.get('@type')
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {str(item).casefold() for item in value}
    return set()


def _safe_price(value):
    text = str(value).strip().replace(',', '')
    return text if _PRICE.fullmatch(text) else None


def _safe_currency(value):
    text = str(value).strip().upper()
    return text if _CURRENCY.fullmatch(text) else None


def extract_product_facts(html: str) -> str:
    """返回可归属来源的产品/报价行，不包含任意脚本文本。"""
    parser = _StructuredDataParser()
    parser.feed((html or '')[:MAX_HTML_CHARS])
    facts: list[str] = []
    seen: set[str] = set()

    def add(value):
        value = normalized_text(value)[:240]
        if value and value not in seen and len(facts) < MAX_FACTS:
            seen.add(value)
            facts.append(value)

    for script in parser.scripts:
        try:
            payload = json.loads(script)
        except (TypeError, ValueError):
            continue
        for node in _nodes(payload):
            if 'product' not in _types(node):
                continue
            name = normalized_text(str(node.get('name') or ''))[:160]
            if name:
                add('Product: ' + name)
            offers = node.get('offers')
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                currency = _safe_currency(offer.get('priceCurrency'))
                for label, field in (('Price', 'price'), ('Low price', 'lowPrice'), ('High price', 'highPrice')):
                    price = _safe_price(offer.get(field))
                    if currency and price:
                        add(f'{label}: {currency} {price}')
                availability = normalized_text(str(offer.get('availability') or '')).rsplit('/', 1)[-1][:80]
                if availability:
                    add('Availability: ' + availability)

    if not any(item.startswith(('Price:', 'Low price:', 'High price:')) for item in facts):
        price = _safe_price(parser.meta.get('product:price:amount'))
        currency = _safe_currency(parser.meta.get('product:price:currency'))
        if price and currency:
            name = parser.meta.get('og:title')
            if name:
                add('Product: ' + name)
            add(f'Price: {currency} {price}')
    return '\n'.join(facts)

