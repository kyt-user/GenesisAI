"""跨层共享的 URL 规范化与文本归一化。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from genesisai.shared.errors import SearchError


TRACKING_PARAMETERS = frozenset({
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src",
})
_SPACE = re.compile(r"\s+")


def normalized_text(value: str) -> str:
    return _SPACE.sub(" ", value or "").strip()


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SearchError("unsafe_url", "Only public HTTP(S) URLs are supported")
    if parsed.username or parsed.password:
        raise SearchError("unsafe_url", "URLs containing credentials are forbidden")
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )):
        raise SearchError("unsafe_url", "Non-standard URL ports are forbidden")
    try:
        netloc = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        netloc = host
    kept = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_PARAMETERS:
            continue
        kept.append((key, item))
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(kept), ""))