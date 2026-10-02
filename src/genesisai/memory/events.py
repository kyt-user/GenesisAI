"""L2 原始事件存储。"""

from __future__ import annotations

import json
import sqlite3
import time

from genesisai.state.store import uid


# events 表 DDL
EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    run_id      TEXT,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    tokens      INTEGER DEFAULT 0,
    created_at  REAL NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
"""

EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);",
    "CREATE INDEX IF NOT EXISTS idx_events_archived ON events(archived);",
]


class EventStore:
    """L2 原始事件存储。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_schema()

    def _ensure_schema(self):
        """确保 events 表存在。"""
        self.conn.execute(EVENTS_DDL)
        for ddl in EVENTS_INDEXES:
            self.conn.execute(ddl)
        self.conn.commit()

    def record_event(self, session_id: str, event_type: str, payload: dict,
                     *, run_id: str | None = None) -> str:
        """记录原始事件。"""
        event_id = uid("evt")
        now = time.time()
        tokens = self._estimate_tokens(payload)
        self.conn.execute(
            """INSERT INTO events (id, session_id, run_id, event_type, payload, tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, session_id, run_id, event_type, json.dumps(payload, ensure_ascii=False), tokens, now)
        )
        self.conn.commit()
        return event_id

    def query_events(self, session_id: str, *, event_type: str | None = None,
                     limit: int = 50) -> list[dict]:
        """查询事件。"""
        sql = "SELECT * FROM events WHERE session_id = ? AND archived = 0"
        params: list = [session_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def archive_old_events(self, days: int = 30) -> int:
        """归档超过指定天数的旧事件。"""
        cutoff = time.time() - days * 86400
        cursor = self.conn.execute(
            "UPDATE events SET archived = 1 WHERE created_at < ? AND archived = 0",
            (cutoff,)
        )
        self.conn.commit()
        return cursor.rowcount

    def _estimate_tokens(self, payload: dict) -> int:
        """粗略估算 payload 的 token 数。"""
        text = json.dumps(payload, ensure_ascii=False)
        return len(text) // 4  # 粗略估算

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """将 Row 转换为 dict。"""
        d = dict(row)
        if "payload" in d and isinstance(d["payload"], str):
            try:
                d["payload"] = json.loads(d["payload"])
            except json.JSONDecodeError:
                d["payload"] = {}
        if "archived" in d:
            d["archived"] = bool(d["archived"])
        return d
