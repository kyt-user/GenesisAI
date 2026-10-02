"""搜索提供商必须实现的最小契约。"""

from abc import ABC, abstractmethod

from genesisai.capabilities.web.contracts import SearchHit, SearchQuery


class SearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, query: SearchQuery, limit: int = 10) -> list[SearchHit]:
        """返回按相关性排序的搜索结果。"""

