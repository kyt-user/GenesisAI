"""Extract all structured blocks from a resource already bounded by network budgets."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from genesisai.core.extensions.tools.web.contracts import normalized_text


_PRICE = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,4})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class _StructuredDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.meta: dict[str, str] = {}
        self._json_ld = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == 'script' and str(values.get('type', '')).lower().split(';', 1)[0].strip() == 'application/ld+json':
            self._json_ld = True
            self._parts = []
        elif tag.lower() == 'meta':
            name = str(values.get('property') or values.get('name') or '').lower()
            if name in {'product:price:amount', 'product:price:currency', 'og:title'}:
                self.meta[name] = normalized_text(str(values.get('content') or ''))

    def handle_data(self, data):
        if self._json_ld:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == 'script' and self._json_ld:
            self.scripts.append(''.join(self._parts))
            self._json_ld = False
            self._parts = []


def _nodes(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))


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


def _facts(payload, meta):
    facts: list[str] = []
    seen: set[str] = set()

    def add(value):
        value = normalized_text(value)
        if value and value not in seen:
            seen.add(value)
            facts.append(value)

    if payload is not None:
        for node in _nodes(payload):
            if 'product' not in _types(node):
                continue
            name = normalized_text(str(node.get('name') or ''))
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
                availability = normalized_text(str(offer.get('availability') or '')).rsplit('/', 1)[-1]
                if availability:
                    add('Availability: ' + availability)

    if not any(item.startswith(('Price:', 'Low price:', 'High price:')) for item in facts):
        price = _safe_price(meta.get('product:price:amount'))
        currency = _safe_currency(meta.get('product:price:currency'))
        if price and currency:
            name = meta.get('og:title')
            if name:
                add('Product: ' + name)
            add(f'Price: {currency} {price}')
    return facts


def extract_structured_blocks(html):
    parser = _StructuredDataParser()
    parser.feed(html or '')
    parser.close()
    blocks = []
    for raw in parser.scripts:
        try:
            payload = json.loads(raw)
            facts, error = _facts(payload, {}), None
        except (ValueError, TypeError, RecursionError):
            facts, error = [], 'invalid_json_ld'
        blocks.append({'index': len(blocks), 'kind': 'json_ld', 'raw': raw, 'facts': facts, 'error': error})
    if parser.meta:
        blocks.append({'index': len(blocks), 'kind': 'product_meta', 'raw': json.dumps(parser.meta, ensure_ascii=False),
                       'facts': _facts(None, parser.meta), 'error': None})
    return blocks


def extract_product_facts(html: str) -> str:
    return product_summary(extract_structured_blocks(html))


def product_summary(blocks):
    primary = [fact for block in blocks if block['kind'] == 'json_ld' for fact in block['facts']]
    # Meta blocks remain fully readable; the main synopsis uses them only when
    # no JSON-LD offer supplies a price, avoiding merging conflicting offers.
    if not any(fact.startswith(('Price:', 'Low price:', 'High price:')) for fact in primary):
        primary.extend(fact for block in blocks if block['kind'] == 'product_meta' for fact in block['facts'])
    return '\n'.join(dict.fromkeys(primary))

