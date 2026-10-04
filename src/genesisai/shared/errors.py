"""跨层共享的稳定错误类型。"""

from __future__ import annotations


class SearchError(RuntimeError):
    def __init__(self, error_type: str, message: str, *, retryable: bool = False, reason: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.reason = reason