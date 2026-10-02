"""SQLite Schema 定义、数据库初始化和 JSON→SQLite 迁移。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

CURRENT_SCHEMA_VERSION = 2

# 与 manager.py 保持一致的常量（避免循环导入）
MEMORY_TYPES = frozenset({
    "project", "architecture", "convention", "command", "decision", "issue",
    "dependency", "gotcha", "character", "world_setting", "plot_thread", "style_rule",
})
SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)\s*[:=]\s*\S+")

# ── 核心表 DDL ──────────────────────────────────────────────

MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    content         TEXT NOT NULL,
    keywords        TEXT NOT NULL DEFAULT '[]',
    scope           TEXT NOT NULL DEFAULT 'project',
    source_path     TEXT,
    source_sha      TEXT,
    verified        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',
    importance      REAL NOT NULL DEFAULT 0.5,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_used_at    REAL,
    run_id          TEXT
);
"""

MEMORIES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_memories_status_type ON memories(status, type);",
    "CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_memories_last_used ON memories(last_used_at);",
    "CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_path) WHERE source_path IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_memories_lifecycle ON memories(status, updated_at) WHERE status IN ('active', 'archived');",
]

# ── FTS5 全文检索虚拟表 ──────────────────────────────────────

FTS5_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title,
    summary,
    content,
    keywords,
    content='memories',
    content_rowid='rowid',
    tokenize='unicode61'
);
"""

FTS5_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS trg_memories_fts_insert
    AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, title, summary, content, keywords)
        VALUES (new.rowid, new.title, new.summary, new.content, new.keywords);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_memories_fts_delete
    AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, summary, content, keywords)
        VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.keywords);
    END;""",
    """CREATE TRIGGER IF NOT EXISTS trg_memories_fts_update
    AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, title, summary, content, keywords)
        VALUES ('delete', old.rowid, old.title, old.summary, old.content, old.keywords);
        INSERT INTO memories_fts(rowid, title, summary, content, keywords)
        VALUES (new.rowid, new.title, new.summary, new.content, new.keywords);
    END;""",
]

# ── 向量缓存表（P10 使用） ──────────────────────────────────

EMBEDDINGS_DDL = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id    TEXT PRIMARY KEY,
    embedding    BLOB NOT NULL,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    created_at   REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
"""

EMBEDDINGS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_embeddings_model ON memory_embeddings(model);",
]

# ── 操作日志表 ──────────────────────────────────────────────

CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS memory_changes (
    id          TEXT PRIMARY KEY,
    operation   TEXT NOT NULL,
    memory_id   TEXT,
    run_id      TEXT,
    details     TEXT DEFAULT '{}',
    created_at  REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
"""

CHANGES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_changes_time ON memory_changes(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_changes_memory ON memory_changes(memory_id, created_at DESC);",
]

# ── Schema 版本表 ──────────────────────────────────────────

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  REAL NOT NULL
);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """将 memory_changes.memory_id 从 NOT NULL 改为允许 NULL。

    SQLite 不支持 ALTER COLUMN，因此需要重建表。
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("ALTER TABLE memory_changes RENAME TO memory_changes_old")
        conn.execute(CHANGES_DDL)
        for ddl in CHANGES_INDEXES:
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at) "
            "SELECT id, operation, "
            "CASE WHEN memory_id = 'system' THEN NULL ELSE memory_id END, "
            "run_id, details, created_at FROM memory_changes_old"
        )
        conn.execute("DROP TABLE memory_changes_old")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("UPDATE schema_version SET version = 2")


def create_connection(db_path: str | Path) -> sqlite3.Connection:
    """创建配置好的 SQLite 连接。"""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-8000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等创建所有表结构、索引和触发器，并执行增量迁移。"""
    conn.execute(SCHEMA_VERSION_DDL)
    conn.execute(MEMORIES_DDL)
    for ddl in MEMORIES_INDEXES:
        conn.execute(ddl)
    conn.execute(FTS5_DDL)
    for ddl in FTS5_TRIGGERS:
        conn.execute(ddl)
    conn.execute(EMBEDDINGS_DDL)
    for ddl in EMBEDDINGS_INDEXES:
        conn.execute(ddl)
    conn.execute(CHANGES_DDL)
    for ddl in CHANGES_INDEXES:
        conn.execute(ddl)
    # 记录 schema 版本（仅首次）
    row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    if row[0] == 0:
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (CURRENT_SCHEMA_VERSION, time.time()),
        )
        conn.commit()
        return
    # 增量迁移：v1 → v2（memory_changes.memory_id 允许 NULL）
    current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    if current < 2:
        _migrate_v1_to_v2(conn)
    conn.commit()


def migrate_from_json(json_dir: Path, db_path: Path) -> dict:
    """从 JSON + Markdown 格式迁移到 SQLite。

    返回 {"entries": int, "changes": int, "backup": Path} 或抛出异常。
    迁移失败时自动回滚。
    """
    json_dir = Path(json_dir)
    db_path = Path(db_path)
    index_path = json_dir / "index.json"

    if not index_path.exists():
        # 没有旧数据，直接初始化空数据库
        conn = create_connection(db_path)
        ensure_schema(conn)
        conn.close()
        return {"entries": 0, "changes": 0, "backup": None}

    # 读取旧索引
    raw = index_path.read_text(encoding="utf-8")
    old_data = json.loads(raw)
    if not isinstance(old_data, dict) or "entries" not in old_data:
        raise ValueError("旧 Memory 索引格式无效")

    old_entries = old_data.get("entries", [])
    old_changes = old_data.get("changes", [])

    # 创建数据库并迁移
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = create_connection(db_path)
    try:
        ensure_schema(conn)

        # 迁移 entries
        for entry in old_entries:
            entry_id = entry["id"]
            # 读取 Markdown 正文
            content_path = json_dir / entry.get("content_path", "")
            content = ""
            if content_path.is_file():
                raw_content = content_path.read_text(encoding="utf-8")
                # 去除 Markdown 标题行 "# title\n\n"
                if raw_content.startswith("# "):
                    newline_pos = raw_content.find("\n")
                    if newline_pos > 0:
                        content = raw_content[newline_pos + 1:].strip()
                    else:
                        content = raw_content
                else:
                    content = raw_content.strip()
            else:
                content = entry.get("summary", "")

            keywords = entry.get("keywords", [])
            if isinstance(keywords, list):
                keywords_json = json.dumps(keywords, ensure_ascii=False)
            else:
                keywords_json = "[]"

            conn.execute(
                """INSERT OR IGNORE INTO memories
                   (id, type, title, summary, content, keywords, scope,
                    source_path, source_sha, verified, status, importance,
                    access_count, created_at, updated_at, last_used_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    entry["type"],
                    entry["title"],
                    entry["summary"],
                    content,
                    keywords_json,
                    entry.get("scope", "project"),
                    entry.get("source_path"),
                    entry.get("source_sha256"),
                    1 if entry.get("verified") else 0,
                    entry.get("status", "active"),
                    entry.get("importance", 0.5),
                    entry.get("access_count", 0),
                    entry["created_at"],
                    entry["updated_at"],
                    entry.get("last_used_at"),
                    entry.get("run_id"),
                ),
            )

        # 迁移 changes
        for change in old_changes:
            conn.execute(
                """INSERT OR IGNORE INTO memory_changes
                   (id, operation, memory_id, run_id, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    change["id"],
                    change["operation"],
                    change["entry_id"],
                    change.get("run_id"),
                    "{}",
                    change["at"],
                ),
            )

        conn.commit()

        # 校验条目数
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if count != len(old_entries):
            conn.close()
            db_path.unlink(missing_ok=True)
            raise ValueError(f"迁移校验失败：期望 {len(old_entries)} 条，实际 {count} 条")

        # 重命名旧文件为备份
        backup_path = json_dir / "index.json.migrated"
        index_path.rename(backup_path)

        conn.close()
        return {"entries": count, "changes": len(old_changes), "backup": backup_path}

    except Exception:
        conn.close()
        db_path.unlink(missing_ok=True)
        raise
