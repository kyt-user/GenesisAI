"""记忆生命周期管理：归档、遗忘、物理删除。"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

from genesisai.state.store import uid


def calculate_strength(memory: dict, now: float) -> float:
    """计算记忆强度。

    公式：strength = importance × 0.4 + frequency_factor × 0.3 + decay_factor × 0.3

    其中：
    - importance: 记忆创建时设定的重要度（0~1）
    - frequency_factor: 访问频率因子，基于 access_count 和创建时长
    - decay_factor: 时间衰减因子，基于半衰期模型
    """
    importance = memory.get("importance", 0.5)

    # 频率因子：归一化访问频率（对数缩放）
    age_days = max(1.0, (now - memory["created_at"]) / 86400)
    access_count = memory.get("access_count", 0)
    frequency_factor = min(1.0, math.log(1 + access_count) / math.log(1 + age_days * 0.5))

    # 衰减因子：指数衰减，半衰期随重要度变化
    last_used = memory.get("last_used_at") or memory["created_at"]
    idle_days = (now - last_used) / 86400
    half_life = 7 + importance * 53  # 7天（importance=0）~ 60天（importance=1）
    decay_factor = 0.5 ** (idle_days / half_life)

    return importance * 0.4 + frequency_factor * 0.3 + decay_factor * 0.3


class LifecycleManager:
    """记忆生命周期管理器。"""

    def run_maintenance(self, conn: sqlite3.Connection, *, run_id: str | None = None) -> dict:
        """执行定期维护：归档、遗忘、验证。"""
        now = time.time()

        # 1. 归档低强度记忆（strength < 0.2 且 30 天未使用）
        archived = self._archive_weak_memories(conn, now, run_id=run_id)

        # 2. 物理删除过期遗忘记忆（forgotten 超过 30 天）
        deleted = self._purge_expired_forgotten(conn, now, run_id=run_id)

        # 3. 验证来源文件有效性
        invalidated = self._validate_sources(conn, run_id=run_id)

        # 4. 记录维护日志
        self._log_maintenance(conn, archived, deleted, invalidated, run_id)

        return {"archived": archived, "deleted": deleted, "invalidated": invalidated}

    def _archive_weak_memories(self, conn: sqlite3.Connection, now: float, *, run_id: str | None) -> int:
        """归档低强度且长期未使用的记忆。"""
        thirty_days_ago = now - 30 * 86400
        rows = conn.execute("""
            SELECT id, importance, access_count, created_at, last_used_at
            FROM memories WHERE status = 'active'
        """).fetchall()
        to_archive = []
        for row in rows:
            memory = dict(row)
            strength = calculate_strength(memory, now)
            last_used = row["last_used_at"] or row["created_at"]
            if strength < 0.2 and last_used < thirty_days_ago:
                to_archive.append(row["id"])
        if to_archive:
            conn.executemany(
                "UPDATE memories SET status='archived', updated_at=? WHERE id=?",
                [(now, mid) for mid in to_archive]
            )
            conn.commit()
        return len(to_archive)

    def _purge_expired_forgotten(self, conn: sqlite3.Connection, now: float, *, run_id: str | None) -> int:
        """物理删除过期遗忘记忆。"""
        thirty_days_ago = now - 30 * 86400
        cursor = conn.execute("""
            DELETE FROM memories WHERE status = 'forgotten' AND updated_at < ?
        """, (thirty_days_ago,))
        conn.commit()
        return cursor.rowcount

    def _validate_sources(self, conn: sqlite3.Connection, *, run_id: str | None) -> int:
        """验证来源文件有效性。"""
        rows = conn.execute("""
            SELECT id, source_path, source_sha, verified
            FROM memories WHERE status = 'active' AND source_path IS NOT NULL
        """).fetchall()
        invalidated = 0
        now = time.time()
        for row in rows:
            source = Path(row["source_path"])
            valid = source.is_file()
            if not valid and row["verified"]:
                conn.execute(
                    "UPDATE memories SET verified=0, updated_at=? WHERE id=?",
                    (now, row["id"])
                )
                invalidated += 1
        if invalidated:
            conn.commit()
        return invalidated

    def _log_maintenance(self, conn: sqlite3.Connection, archived: int, deleted: int,
                         invalidated: int, run_id: str | None) -> None:
        """记录维护操作日志。"""
        now = time.time()
        details = {"archived": archived, "deleted": deleted, "invalidated": invalidated}
        import json
        conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "maintenance", None, run_id, json.dumps(details), now)
        )
        conn.commit()

    def forget(self, conn: sqlite3.Connection, memory_id: str, *, run_id: str | None = None) -> dict:
        """手动遗忘记忆。"""
        now = time.time()
        conn.execute(
            "UPDATE memories SET status='forgotten', updated_at=? WHERE id=? AND status='active'",
            (now, memory_id)
        )
        conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "forget", memory_id, run_id, "{}", now)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else {}

    def restore(self, conn: sqlite3.Connection, memory_id: str, *, run_id: str | None = None) -> dict:
        """手动恢复已归档/遗忘的记忆。"""
        now = time.time()
        conn.execute(
            "UPDATE memories SET status='active', updated_at=? WHERE id=? AND status IN ('archived', 'forgotten')",
            (now, memory_id)
        )
        conn.execute(
            """INSERT INTO memory_changes (id, operation, memory_id, run_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid("memchange"), "restore", memory_id, run_id, "{}", now)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return dict(row) if row else {}

    def get_strength(self, conn: sqlite3.Connection, memory_id: str) -> float:
        """查询单条记忆的当前强度。"""
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return 0.0
        return calculate_strength(dict(row), time.time())
