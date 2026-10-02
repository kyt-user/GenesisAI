"""增强写入管线：去重、合并。"""

from __future__ import annotations

import json
import sqlite3
import time

from genesisai.state.store import uid


class EnhancedWriter:
    """增强写入器：精确去重 + 内容合并。"""

    def write(self, conn: sqlite3.Connection, *, type: str, title: str, summary: str,
              content: str, keywords: list[str], scope: str, source_path: str | None,
              source_sha: str | None, verified: bool, run_id: str | None) -> dict:
        """写入记忆，先检查去重。"""
        # 精确去重检查
        existing = self.check_exact_duplicate(conn, title, type)
        if existing:
            # 合并内容
            merged = self.merge_memory_content(existing, content, summary, keywords)
            now = time.time()
            conn.execute(
                """UPDATE memories SET summary=?, content=?, keywords=?, updated_at=?
                   WHERE id=?""",
                (merged["summary"], merged["content"], merged["keywords"], now, existing["id"])
            )
            conn.execute(
                """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uid("memchange"), "merge", existing["id"], run_id, "{}", now)
            )
            conn.commit()
            return {**existing, **merged, "updated_at": now}

        # 无重复，直接插入
        identifier = uid("mem")
        now = time.time()
        keywords_json = json.dumps(sorted(set(keywords)), ensure_ascii=False)
        conn.execute(
            """INSERT INTO memories
               (id, type, title, summary, content, keywords, scope,
                source_path, source_sha, verified, status, importance,
                access_count, created_at, updated_at, last_used_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (identifier, type, title, summary, content, keywords_json, scope,
             source_path, source_sha, 1 if verified else 0, "active", 0.5, 0, now, now, None, run_id)
        )
        conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "add", identifier, run_id, "{}", now)
        )
        conn.commit()
        return {
            "id": identifier, "type": type, "title": title, "summary": summary,
            "keywords": sorted(set(keywords)), "scope": scope, "status": "active",
            "created_at": now, "updated_at": now,
        }

    def check_exact_duplicate(self, conn: sqlite3.Connection, title: str, type: str) -> dict | None:
        """检查是否存在相同 title + type 的活跃记忆。"""
        row = conn.execute("""
            SELECT * FROM memories
            WHERE title = ? AND type = ? AND status = 'active'
        """, (title, type)).fetchone()
        return dict(row) if row else None

    def check_semantic_duplicate(self, conn: sqlite3.Connection, new_embedding: list[float],
                                  threshold: float = 0.90) -> tuple[str, float] | None:
        """检查是否存在语义相似的记忆（P10 阶段启用）。"""
        # 预留接口，P10 阶段实现
        return None

    def merge_memory_content(self, existing: dict, new_content: str, new_summary: str,
                             new_keywords: list[str]) -> dict:
        """将新信息合并到已有记忆中。"""
        merged = dict(existing)
        # 合并关键词（取并集）
        old_kw = set(json.loads(existing.get("keywords", "[]")) if isinstance(existing.get("keywords"), str) else existing.get("keywords", []))
        merged_kw = sorted(old_kw | set(new_keywords))
        merged["keywords"] = json.dumps(merged_kw, ensure_ascii=False)
        # 追加新内容（如果不同）
        old_content = existing.get("content", "")
        if new_content.strip() not in old_content:
            merged["content"] = old_content.rstrip() + "\n\n" + new_content.strip()
        else:
            merged["content"] = old_content
        # 更新摘要（如果新摘要更丰富）
        old_summary = existing.get("summary", "")
        if len(new_summary) > len(old_summary):
            merged["summary"] = new_summary
        else:
            merged["summary"] = old_summary
        return merged
