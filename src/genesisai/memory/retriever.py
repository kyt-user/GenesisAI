"""FTS5 全文检索器。"""

from __future__ import annotations

import re
import sqlite3
from typing import Any


class FTS5Retriever:
    """基于 FTS5 的关键词检索器。"""

    def search(self, conn: sqlite3.Connection, query: str, *,
               type: str | None = None, limit: int = 20) -> list[dict]:
        """执行 FTS5 全文检索。"""
        limit = max(1, min(limit, 100))
        
        if not query or not query.strip():
            # 空查询返回所有活跃记忆
            sql = "SELECT * FROM memories WHERE status = 'active'"
            params: list = []
            if type:
                sql += " AND type = ?"
                params.append(type)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(row) for row in rows]

        # 先尝试 FTS5 检索
        fts_query = self._build_fts_query(query)
        results = []
        if fts_query:
            sql = """
                SELECT m.*, bm25(memories_fts, 10.0, 5.0, 1.0, 3.0) AS score
                FROM memories_fts fts
                JOIN memories m ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ?
                  AND m.status = 'active'
            """
            params = [fts_query]
            if type:
                sql += " AND m.type = ?"
                params.append(type)
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
                results = [self._row_to_dict(row) for row in rows]
            except Exception:
                # FTS5 查询失败，回退到 LIKE 查询
                pass

        # 如果 FTS5 没有结果，使用 LIKE 回退（对中文更友好）
        if not results:
            results = self._like_search(conn, query, type=type, limit=limit)

        return results

    def _build_fts_query(self, query: str) -> str:
        """将用户查询转换为 FTS5 MATCH 语法。

        处理策略：
        - 分离中文和英文/数字部分
        - 中文部分：利用 unicode61 自动按字分词，使用短语匹配
        - 英文部分：使用双引号精确匹配 + 前缀匹配
        - 组合为 OR 表达式以提高召回率
        """
        query = query.strip()
        if not query:
            return ""

        # 分离中文和英文/数字部分
        chinese_parts = re.findall(r'[\u4e00-\u9fff]+', query)
        english_parts = re.findall(r'[a-zA-Z0-9_\-\.]+', query)

        terms: list[str] = []

        # 处理英文部分：精确匹配 + 前缀匹配
        for part in english_parts:
            part = part.strip()
            if not part:
                continue
            # 转义特殊字符
            escaped = part.replace('"', '""')
            # 使用双引号精确匹配
            terms.append(f'"{escaped}"')

        # 处理中文部分：直接拼接（unicode61 会自动按字分词）
        for part in chinese_parts:
            if part:
                # 中文使用短语匹配（双引号包裹）
                terms.append(f'"{part}"')

        if not terms:
            # 如果无法提取有效词，尝试原样查询
            # 转义 FTS5 特殊字符
            safe_query = re.sub(r'[*"()+]', '', query)
            if safe_query.strip():
                return f'"{safe_query.strip()}"'
            return ""

        # 使用 OR 组合所有词（提高召回率）
        return " OR ".join(terms)

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 SQLite Row 转换为 dict。"""
        import json
        d = dict(row)
        # 兼容旧字段名
        if "source_sha" in d:
            d["source_sha256"] = d.pop("source_sha")
        if "keywords" in d and isinstance(d["keywords"], str):
            try:
                d["keywords"] = json.loads(d["keywords"])
            except (json.JSONDecodeError, TypeError):
                d["keywords"] = []
        if "verified" in d:
            d["verified"] = bool(d["verified"])
        return d

    def _like_search(self, conn: sqlite3.Connection, query: str, *,
                     type: str | None = None, limit: int = 20) -> list[dict]:
        """使用 LIKE 模糊搜索（对中文更友好）。"""
        # 提取查询词
        words = [w for w in re.split(r'\s+', query.strip()) if w]
        if not words:
            return []
        
        conditions = []
        params: list = []
        for word in words:
            conditions.append(
                "(title LIKE ? OR summary LIKE ? OR content LIKE ? OR keywords LIKE ?)"
            )
            pattern = f"%{word}%"
            params.extend([pattern, pattern, pattern, pattern])
        
        sql = f"SELECT * FROM memories WHERE status = 'active' AND ({' AND '.join(conditions)})"
        if type:
            sql += " AND type = ?"
            params.append(type)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(row) for row in rows]
