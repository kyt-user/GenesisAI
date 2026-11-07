"""搜索提供商必须实现的最小契约。"""

from abc import ABC, abstractmethod
from functools import wraps

from genesisai.core.extensions.tools.web.contracts import SearchPage, SearchQuery
from genesisai.core.extensions.tools.web.budget import NetworkLimits, active_limits, network_budget


def bounded_search(action):
    @wraps(action)
    def execute(self, query, limit=10):
        limits = NetworkLimits.parse(query.network_limits if query is not None else None)
        token = active_limits.set(limits)
        try:
            with network_budget(limits):
                return action(self, query, limit)
        finally:
            active_limits.reset(token)
    return execute


class SearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: SearchQuery, limit: int = 10) -> SearchPage:
        """Return hits and a provider-supported continuation, never a guessed cursor."""

