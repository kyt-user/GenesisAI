"""单 Agent 搜索链使用的查询、结果与 URL 契约。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_QUERY_CHARS = 400
MAX_QUERY_WORDS = 50
TRACKING_PARAMETERS = frozenset({
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src",
})
_SPACE = re.compile(r"\s+")
SAFE_PROVIDER_ERROR_CODES = frozenset({
    "provider_auth_error", "provider_rate_limited", "provider_unavailable",
    "provider_invalid_response", "access_denied",
})
SAFE_PROVIDER_REASONS = frozenset({
    "missing_credentials", "authentication_rejected", "timeout",
    "connection_error", "http_status", "rate_limit", "parse_error",
    "access_denied", "invalid_response", "endpoint_rejected",
})


class SearchError(RuntimeError):
    def __init__(self, error_type: str, message: str, *, retryable: bool = False, reason: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.reason = reason


def classify_transport_error(exc: Exception) -> tuple[str, str, bool] | None:
    """将预期的传输层故障映射为稳定、不泄露敏感信息的诊断。"""
    raw_code = getattr(exc, "error_type", None)
    class_name = type(exc).__name__
    module = type(exc).__module__
    if raw_code == "timeout" or isinstance(exc, TimeoutError) or "Timeout" in class_name:
        return "provider_unavailable", "timeout", True
    if raw_code == "unsafe_url":
        return "provider_unavailable", "endpoint_rejected", False
    if raw_code in {"invalid_response", "unsupported_encoding", "size_limit", "redirect_limit"}:
        return "provider_invalid_response", "invalid_response", False
    if isinstance(exc, (ConnectionError, OSError)) or module.startswith("httpx"):
        return "provider_unavailable", "connection_error", True
    return None


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


@dataclass(frozen=True)
class SearchQuery:
    query: str
    purpose: str = "general"
    priority: int = 1
    language: str | None = None
    country: str | None = None
    freshness: str | None = None
    allowed_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        query = normalized_text(self.query)
        if not query or len(query) > MAX_QUERY_CHARS or len(query.split()) > MAX_QUERY_WORDS:
            raise SearchError("validation_error", "Search query exceeds the allowed size")
        if self.priority not in {1, 2, 3}:
            raise SearchError("validation_error", "Query priority must be 1, 2, or 3")
        object.__setattr__(self, "query", query)
        object.__setattr__(
            self,
            "allowed_domains",
            tuple(item.lower().rstrip(".") for item in self.allowed_domains if item),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["allowed_domains"] = list(self.allowed_domains)
        return {key: item for key, item in value.items() if item is not None}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchQuery":
        allowed = {
            "query", "purpose", "priority", "language", "country", "freshness",
            "allowed_domains",
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise SearchError("model_output_invalid", "Research query schema is invalid")
        return cls(
            query=str(value.get("query", "")),
            purpose=str(value.get("purpose", "general")),
            priority=int(value.get("priority", 1)),
            language=value.get("language"),
            country=value.get("country"),
            freshness=value.get("freshness"),
            allowed_domains=tuple(value.get("allowed_domains", ())),
        )


@dataclass(frozen=True)
class SearchHit:
    provider: str
    rank: int
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    source_type_hint: str = "web_page"

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", canonicalize_url(self.url))
        if self.rank < 1:
            raise SearchError("provider_invalid_response", "Search rank must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SearchHit":
        return cls(**value)
