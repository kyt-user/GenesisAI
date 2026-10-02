"""L4 情景记忆管理。"""

from __future__ import annotations

import json
import sqlite3
import time

from genesisai.state.store import uid


# episodes 表 DDL
EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general',
    outcome         TEXT NOT NULL DEFAULT 'neutral',
    summary         TEXT NOT NULL,
    key_decisions   TEXT NOT NULL DEFAULT '[]',
    lessons         TEXT NOT NULL DEFAULT '[]',
    tags            TEXT NOT NULL DEFAULT '[]',
    related_memories TEXT NOT NULL DEFAULT '[]',
    importance      REAL NOT NULL DEFAULT 0.5,
    access_count    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_used_at    REAL
);
"""

EPISODES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_episodes_category ON episodes(category);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_outcome ON episodes(outcome);",
    "CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);",
]

# episodes FTS5 虚拟表
EPISODES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    title,
    summary,
    tags,
    content='episodes',
    content_rowid='rowid',
    tokenize='unicode61'
);
"""

EPISODES_FTS_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS trg_episodes_fts_insert
    AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, title, summary, tags)
        VALUES (new.rowid, new.title, new.summary, new.tags);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_episodes_fts_delete
    AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, summary, tags)
        VALUES ('delete', old.rowid, old.title, old.summary, old.tags);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_episodes_fts_update
    AFTER UPDATE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, title, summary, tags)
        VALUES ('delete', old.rowid, old.title, old.summary, old.tags);
        INSERT INTO episodes_fts(rowid, title, summary, tags)
        VALUES (new.rowid, new.title, new.summary, new.tags);
    END;""",
]


class EpisodicMemory:
    """L4 情景记忆管理。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_schema()

    def _ensure_schema(self):
        """确保 episodes 表和 FTS 索引存在。"""
        self.conn.execute(EPISODES_DDL)
        for ddl in EPISODES_INDEXES:
            self.conn.execute(ddl)
        self.conn.execute(EPISODES_FTS_DDL)
        for ddl in EPISODES_FTS_TRIGGERS:
            self.conn.execute(ddl)
        self.conn.commit()

    def extract_episode(self, session_id: str, events: list[dict], *,
                        title: str = "", category: str = "general") -> str:
        """从会话事件中提取情景记忆。"""
        episode_id = uid("ep")
        now = time.time()
        
        # 从事件中提取摘要和教训
        summary = self._extract_summary(events)
        lessons = self._extract_lessons(events)
        key_decisions = self._extract_decisions(events)
        tags = self._extract_tags(events)
        outcome = self._determine_outcome(events)

        self.conn.execute(
            """INSERT INTO episodes
               (id, session_id, title, category, outcome, summary,
                key_decisions, lessons, tags, related_memories, importance,
                access_count, status, created_at, updated_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, session_id, title or f"会话 {session_id[:8]}", category,
             outcome, summary, json.dumps(key_decisions, ensure_ascii=False),
             json.dumps(lessons, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
             "[]", 0.5, 0, "active", now, now, None)
        )
        self.conn.commit()
        return episode_id

    def search_episodes(self, query: str = "", *, category: str | None = None,
                        outcome: str | None = None, limit: int = 20) -> list[dict]:
        """搜索情景记忆。"""
        if query:
            # 使用 FTS5 搜索
            sql = """
                SELECT e.* FROM episodes_fts fts
                JOIN episodes e ON e.rowid = fts.rowid
                WHERE episodes_fts MATCH ?
                  AND e.status = 'active'
            """
            params: list = [query]
        else:
            sql = "SELECT * FROM episodes WHERE status = 'active'"
            params = []

        if category:
            sql += " AND category = ?"
            params.append(category)
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _extract_summary(self, events: list[dict]) -> str:
        """从事件中提取摘要。"""
        user_msgs = [e for e in events if e.get("event_type") == "user_message"]
        if user_msgs:
            return user_msgs[0].get("payload", {}).get("content", "")[:200]
        return "无摘要"

    def _extract_lessons(self, events: list[dict]) -> list[str]:
        """从事件中提取教训。"""
        lessons = []
        for e in events:
            if e.get("event_type") == "execution_result":
                payload = e.get("payload", {})
                if payload.get("success") is False:
                    lessons.append(f"失败: {payload.get('error', '未知错误')[:100]}")
        return lessons[:3]

    def _extract_decisions(self, events: list[dict]) -> list[str]:
        """从事件中提取关键决策。"""
        decisions = []
        for e in events:
            if e.get("event_type") == "tool_call":
                tool = e.get("payload", {}).get("tool", "")
                if tool in ["file_write", "file_edit"]:
                    decisions.append(f"修改文件: {tool}")
        return decisions[:3]

    def _extract_tags(self, events: list[dict]) -> list[str]:
        """从事件中提取标签。"""
        tags = set()
        for e in events:
            tool = e.get("payload", {}).get("tool", "")
            if tool:
                tags.add(tool)
        return list(tags)[:5]

    def _determine_outcome(self, events: list[dict]) -> str:
        """确定会话结果。"""
        for e in reversed(events):
            if e.get("event_type") == "execution_result":
                if e.get("payload", {}).get("success"):
                    return "success"
                return "failure"
        return "neutral"

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 Row 转换为 dict。"""
        d = dict(row)
        for field in ["key_decisions", "lessons", "tags", "related_memories"]:
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except json.JSONDecodeError:
                    d[field] = []
        return d
