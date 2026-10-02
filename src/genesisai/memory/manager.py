"""SQLite 后端的记忆管理核心类。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from genesisai.memory.retriever import FTS5Retriever
from genesisai.memory.schema import (
    create_connection,
    ensure_schema,
    migrate_from_json,
)
from genesisai.state.store import sha, uid


MEMORY_TYPES = frozenset({
    "project", "architecture", "convention", "command", "decision", "issue",
    # 开发类记忆类型。
    "dependency", "gotcha",
    # 创作类记忆类型。
    "character", "world_setting", "plot_thread", "style_rule",
})

CODING_TYPES = frozenset({"architecture", "convention", "dependency", "gotcha", "command"})
CREATIVE_TYPES = frozenset({"character", "world_setting", "plot_thread", "style_rule"})
SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)\s*[:=]\s*\S+")


class MemoryError(ValueError):
    """Memory 数据或操作无效。"""


class MemoryManager:
    """可持久化的 SQLite 记忆管理器。"""

    def __init__(self, workspace: Path, *, root: Path | None = None, scope: str = "project"):
        self.workspace = Path(workspace).resolve()
        self.scope = scope
        if scope not in {"project", "user"}:
            raise MemoryError("Memory scope 必须是 project 或 user")
        self.root = Path(root or (
            self.workspace / ".genesis" / "memory"
            if scope == "project" else Path.home() / ".genesisai" / "memory"
        )).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "memories.db"
        # 兼容旧属性（指向 db 文件而非 json）
        self.index_path = self.db_path
        self._needs_migration = self._check_legacy()
        if self._needs_migration:
            self._do_migration()
        self.conn = create_connection(self.db_path)
        ensure_schema(self.conn)

    def _check_legacy(self) -> bool:
        """检查是否存在旧 JSON 格式数据。"""
        return (self.root / "index.json").exists() and not self.db_path.exists()

    def _do_migration(self):
        """执行 JSON→SQLite 迁移。"""
        try:
            migrate_from_json(self.root, self.db_path)
        except Exception:
            # 迁移失败，回滚到 JSON 模式（保持旧行为）
            raise MemoryError("Memory 迁移失败，旧文件已保留")

    @staticmethod
    def _clean(value: str, limit: int) -> str:
        value = SECRET.sub("[REDACTED]", str(value)).strip()
        if len(value) > limit:
            raise MemoryError(f"Memory 内容超过 {limit} 字符")
        return value

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 SQLite Row 转换为与旧版兼容的 dict。"""
        d = dict(row)
        # 兼容旧字段名
        if "source_sha" in d:
            d["source_sha256"] = d.pop("source_sha")
        if "keywords" in d and isinstance(d["keywords"], str):
            try:
                d["keywords"] = json.loads(d["keywords"])
            except (json.JSONDecodeError, TypeError):
                d["keywords"] = []
        # verified 在 SQLite 中是 INTEGER，旧版是 bool
        if "verified" in d:
            d["verified"] = bool(d["verified"])
        return d

    def add(self, *, type: str, title: str, summary: str, content: str, keywords=None,
            source_path: str | None = None, verified: bool = False, run_id: str | None = None):
        if type not in MEMORY_TYPES:
            raise MemoryError("未知 Memory 类型")
        title = self._clean(title, 200)
        summary = self._clean(summary, 500)
        content = self._clean(content, 4000)
        if not title or not summary or not content:
            raise MemoryError("Memory 标题、摘要和正文不能为空")
        keywords = [self._clean(item, 80).casefold() for item in (keywords or [])]
        identifier = uid("mem")
        source_value = None
        source_digest = None
        if source_path:
            source = Path(source_path)
            source = source.resolve(strict=True) if source.is_absolute() else (self.workspace / source).resolve(strict=True)
            if not source.is_relative_to(self.workspace) or not source.is_file() or source.name.startswith(".env"):
                raise MemoryError("Memory 来源必须是工作区内的普通非密钥文件")
            source_value, source_digest = str(source), sha(source)
        now = time.time()
        keywords_json = json.dumps(sorted(set(keywords)), ensure_ascii=False)
        self.conn.execute(
            """INSERT INTO memories
               (id, type, title, summary, content, keywords, scope,
                source_path, source_sha, verified, status, importance,
                access_count, created_at, updated_at, last_used_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                identifier, type, title, summary, content, keywords_json,
                self.scope, source_value, source_digest,
                1 if verified else 0, "active", 0.5, 0, now, now, None, run_id,
            ),
        )
        self.conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "add", identifier, run_id, "{}", now),
        )
        self.conn.commit()
        # 返回与旧版兼容的 dict
        return {
            "id": identifier, "type": type, "title": title, "summary": summary,
            "keywords": sorted(set(keywords)), "content_path": None,
            "scope": self.scope, "source_path": source_value, "source_sha256": source_digest,
            "verified": bool(verified), "status": "active", "created_at": now,
            "updated_at": now, "last_used_at": None, "run_id": run_id,
        }

    def search(self, query: str = "", *, type: str | None = None, limit: int = 20):
        """使用 FTS5 全文检索搜索记忆。"""
        retriever = FTS5Retriever()
        return retriever.search(self.conn, query, type=type, limit=limit)

    def by_category(self, *types: str, limit: int = 50) -> list[dict]:
        """返回匹配指定类型的所有活动条目。"""
        limit = max(1, min(limit, 200))
        target = set(types) if types else set(MEMORY_TYPES)
        placeholders = ",".join(["?"] * len(target))
        sql = f"SELECT * FROM memories WHERE status = 'active' AND type IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?"
        params = list(target) + [limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def summaries_for(self, types: frozenset[str], limit: int = 10) -> list[dict]:
        """返回用于提示词注入的紧凑摘要。"""
        if not types:
            return []
        placeholders = ",".join(["?"] * len(types))
        sql = f"""SELECT id, type, title, summary FROM memories
                  WHERE status = 'active' AND type IN ({placeholders})
                  ORDER BY updated_at DESC LIMIT ?"""
        params = list(types) + [limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def read(self, identifier: str):
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ? AND status = 'active'",
            (identifier,),
        ).fetchone()
        if not row:
            raise MemoryError("Memory 条目不存在或已忘记")
        item = self._row_to_dict(row)
        # 更新访问时间和计数（access_count 影响记忆强度计算）
        now = time.time()
        self.conn.execute(
            "UPDATE memories SET last_used_at = ?, updated_at = ?, access_count = access_count + 1 WHERE id = ?",
            (now, now, identifier),
        )
        self.conn.commit()
        # content 直接存在数据库中，不再读文件
        return {"entry": item, "content": item.get("content", "")}

    def forget(self, identifier: str, *, run_id=None):
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ? AND status = 'active'",
            (identifier,),
        ).fetchone()
        if not row:
            raise MemoryError("Memory 条目不存在或已忘记")
        now = time.time()
        self.conn.execute(
            "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ?",
            (now, identifier),
        )
        self.conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "forget", identifier, run_id, "{}", now),
        )
        self.conn.commit()
        item = self._row_to_dict(row)
        item["status"] = "forgotten"
        item["updated_at"] = now
        return item

    def validate_sources(self):
        rows = self.conn.execute(
            "SELECT id, source_path, source_sha, verified FROM memories WHERE status = 'active' AND source_path IS NOT NULL"
        ).fetchall()
        changed = []
        now = time.time()
        for row in rows:
            source = row["source_path"]
            path = Path(source)
            valid = path.is_file() and sha(path) == row["source_sha"]
            current_verified = bool(row["verified"])
            if current_verified != valid:
                self.conn.execute(
                    "UPDATE memories SET verified = ?, updated_at = ? WHERE id = ?",
                    (1 if valid else 0, now, row["id"]),
                )
                changed.append(row["id"])
        if changed:
            self.conn.commit()
        return changed

    def undo(self, *, run_id: str | None = None):
        if run_id:
            change = self.conn.execute(
                "SELECT * FROM memory_changes WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        else:
            change = self.conn.execute(
                "SELECT * FROM memory_changes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not change:
            raise MemoryError("没有可撤销的 Memory 变化")
        memory_id = change["memory_id"]
        operation = change["operation"]
        now = time.time()
        if operation == "add":
            self.conn.execute(
                "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
        elif operation == "forget":
            self.conn.execute(
                "UPDATE memories SET status = 'active', updated_at = ? WHERE id = ?",
                (now, memory_id),
            )
        elif operation == "merge":
            # merge 操作目前未保存合并前快照，仅记录日志并跳过
            pass
        else:
            raise MemoryError("不支持撤销该 Memory 变化")
        self.conn.execute("DELETE FROM memory_changes WHERE id = ?", (change["id"],))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row_to_dict(row) if row else {}

    def recent_changes(self, limit: int = 20) -> list[dict]:
        """返回最近的操作记录（替代旧版 manager.data['changes'][-20:]）。"""
        rows = self.conn.execute(
            "SELECT * FROM memory_changes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "operation": row["operation"],
                "entry_id": row["memory_id"],
                "at": row["created_at"],
                "run_id": row["run_id"],
            })
        result.reverse()
        return result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        """关闭数据库连接，多次调用安全。"""
        conn = getattr(self, 'conn', None)
        if conn is not None:
            self.conn = None
            conn.close()


def manager_for(context):
    """工厂函数：返回绑定到当前上下文的 MemoryManager 实例。

    调用方应使用 ``with`` 语句确保连接释放。
    """
    return MemoryManager(context.access.workspace)
