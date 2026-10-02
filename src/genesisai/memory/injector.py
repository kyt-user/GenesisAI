"""智能 Prompt 注入器。"""

from __future__ import annotations

import json
import sqlite3
import time

from genesisai.memory.retriever import FTS5Retriever


class MemoryInjector:
    """智能记忆注入器。

    按综合得分选择记忆，在 token 预算内注入。
    """

    TOKEN_BUDGET = 2000  # 记忆注入 token 上限
    TYPE_PRIORITIES = {
        "gotcha": 1.0,
        "convention": 0.9,
        "architecture": 0.85,
        "dependency": 0.8,
        "command": 0.7,
        "project": 0.7,
        "decision": 0.6,
        "issue": 0.6,
        "character": 0.5,
        "world_setting": 0.5,
        "plot_thread": 0.5,
        "style_rule": 0.5,
    }

    def select_memories(self, conn: sqlite3.Connection, query: str, *,
                        limit: int = 10) -> list[dict]:
        """按综合得分选择要注入的记忆。

        综合得分 = retrieval_score × 0.5
                 + importance × 0.2
                 + recency × 0.15
                 + type_priority × 0.15
        """
        # 获取检索结果
        retriever = FTS5Retriever()
        candidates = retriever.search(conn, query, limit=limit * 3)
        now = time.time()
        scored = []
        for mem in candidates:
            retrieval_score = mem.get("score", 1.0) if "score" in mem else 1.0
            importance = mem.get("importance", 0.5)
            # 新鲜度：最近 7 天 = 1.0，30 天 = 0.5，90 天 = 0.1
            age_days = (now - mem.get("updated_at", now)) / 86400
            recency = max(0.0, 1.0 - age_days / 90)
            type_priority = self.TYPE_PRIORITIES.get(mem.get("type", ""), 0.5)

            total = (retrieval_score * 0.5
                     + importance * 0.2
                     + recency * 0.15
                     + type_priority * 0.15)
            scored.append((total, mem))

        scored.sort(key=lambda x: -x[0])

        # 在 token 预算内选择
        selected = []
        tokens_used = 0
        for score, mem in scored:
            mem_tokens = self._estimate_tokens(mem)
            if tokens_used + mem_tokens > self.TOKEN_BUDGET:
                break
            selected.append(mem)
            tokens_used += mem_tokens

        # 更新访问统计
        self._update_access_stats(conn, [m["id"] for m in selected])

        return selected

    def format_injection(self, memories: list[dict]) -> str:
        """格式化记忆注入文本。"""
        if not memories:
            return ""
        lines = ["## 项目记忆（按相关度排序）"]
        for mem in memories:
            mem_type = mem.get("type", "unknown")
            title = mem.get("title", "")
            summary = mem.get("summary", "")
            lines.append(f"- **[{mem_type}]** {title}: {summary}")
        return "\n".join(lines)

    def _estimate_tokens(self, memory: dict) -> int:
        """估算单条记忆的 token 数。"""
        text = f"{memory.get('title', '')} {memory.get('summary', '')}"
        # 粗略估算：中文 1.5 字/token，英文 4 字符/token
        return max(10, len(text) // 3)

    def _update_access_stats(self, conn: sqlite3.Connection, memory_ids: list[str]) -> None:
        """更新访问统计。"""
        if not memory_ids:
            return
        now = time.time()
        for mid in memory_ids:
            conn.execute(
                """UPDATE memories SET access_count = access_count + 1,
                   last_used_at = ? WHERE id = ?""",
                (now, mid)
            )
        conn.commit()
