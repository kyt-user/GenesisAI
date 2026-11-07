"""Project-scoped reads, revisioned knowledge, and exact history references."""

import json
from pathlib import Path
import re
from genesisai.core.persistence.database import Database, encode
from genesisai.core.memory.invalidation import invalidate_sources
from genesisai.core.memory.scope import CodeScope
from genesisai.shared.messages import content_text

class MemoryService:
    def __init__(self, session, code_scope=None):
        self.session = session
        self.database = session.database
        self.project_id = session.record.project_id
        self.workspace_id = session.record.workspace_id
        self.code_scope = code_scope or CodeScope(
            session.workspace, [*session.record.grants, session.record.output]
        )

    def search(self, query, *, corpus="all", limit=8, offset=0):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("搜索词不能为空")
        if corpus not in {"all", "history", "project", "code"}:
            raise ValueError("Unknown search corpus")
        if type(limit) is not int or not 1 <= limit <= 101 or type(offset) is not int or offset < 0:
            raise ValueError('Invalid search range')
        fetch_limit = offset + limit
        scopes = "d.project_id=? AND (d.workspace_id=? OR d.workspace_id IS NULL)"
        args = [self.project_id, self.workspace_id]
        if corpus in {"all", "code"} and not self.code_scope.unrestricted:
            allowed, paths = self.code_scope.sql("d.title")
            scopes += f" AND (d.kind != 'code' OR ({allowed}))"
            args.extend(paths)
        if corpus != "all":
            scopes += " AND d.kind=?"
            args.append({"project": "note"}.get(corpus, corpus))
        terms = re.findall(r"[A-Za-z0-9_./\\-]+|[\u3400-\u9fff]+", query)
        long = [t for t in terms if len(t) >= 3]
        # Chinese queries also recall neighboring character triples.
        for term in list(long):
            if re.fullmatch(r"[\u3400-\u9fff]+", term) and len(term) > 3:
                long.extend(
                    term[i : i + 3] for i in range(0, len(term) - 2, 2)
                )
        found = []
        if long:
            expression = " OR ".join(
                '"' + t.replace('"', '""') + '"' for t in dict.fromkeys(long)
            )
            found = self.database.rows(
                "SELECT d.*,bm25(search_fts) AS rank FROM search_fts JOIN search_documents d ON d.id=search_fts.id WHERE search_fts MATCH ? AND "
                + scopes
                + " ORDER BY rank,d.id LIMIT ?",
                (expression, *args, fetch_limit),
            )
        # Literal matching is an intentional short-token search path, including two-character Chinese.
        if len(found) < fetch_limit and terms:
            predicates = " OR ".join(
                "(instr(lower(d.title),lower(?))>0 OR instr(lower(d.body),lower(?))>0)"
                for _ in terms
            )
            values = [value for term in terms for value in (term, term)]
            found += self.database.rows(
                "SELECT d.* FROM search_documents d WHERE "
                + scopes
                + " AND ("
                + predicates
                + ") ORDER BY d.id LIMIT ?",
                (*args, *values, fetch_limit),
            )
        unique = {row["id"]: row for row in found}
        results = []
        for row in list(unique.values())[offset:offset + limit]:
            item = dict(
                ref=row["id"],
                kind=row["kind"],
                title=row["title"],
                excerpt=self._excerpt(row["body"], terms),
                workspace_id=row["workspace_id"],
            )
            if row["kind"] == "note":
                note = self.database.rows(
                    "SELECT status,revision FROM notes WHERE id=?", (row["id"],)
                )
                if note:
                    item.update(note[0])
            results.append(item)
        return results

    @staticmethod
    def _excerpt(body, terms):
        positions = [body.lower().find(t.lower()) for t in terms]
        start = max(0, min((p for p in positions if p >= 0), default=0) - 120)
        return body[start : start + 1200]

    def read(self, ref, *, offset=0, limit=200000, revision=None, view='record', page_tokens=None):
        if view not in {'record', 'content'}:
            raise ValueError('memory_read view 必须为 record 或 content')
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 200000
        ):
            raise ValueError("Invalid read range")
        if page_tokens is not None and (type(page_tokens) is not int or page_tokens < 1):
            raise ValueError('Invalid page token budget')
        scope = (ref, self.project_id, self.workspace_id)
        notes = self.database.rows(
            "SELECT * FROM notes WHERE id=? AND project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
            scope,
        )
        events = self.database.rows(
            "SELECT e.* FROM events e JOIN sessions s ON e.session_id=s.id WHERE e.id=? AND s.project_id=? AND s.workspace_id=?",
            scope,
        )
        allowed, paths = self.code_scope.sql("path")
        files = self.database.rows(
            f"SELECT * FROM file_snapshots WHERE id=? AND project_id=? AND workspace_id=? AND ({allowed})",
            (*scope, *paths),
        )
        if notes:
            note = notes[0]
            if revision is not None and (type(revision) is not int or revision < 1):
                raise ValueError("Invalid memory revision")
            versions = self.database.rows(
                "SELECT payload FROM note_versions WHERE note_id=? AND revision=?",
                (ref, revision or note["revision"]),
            )
            if not versions:
                raise ValueError("知识版本不存在")
            note = json.loads(versions[0]["payload"])
            body = note["body"]
            metadata = {
                k: note[k]
                for k in (
                    "revision",
                    "status",
                    "kind",
                    "updated",
                    "sources",
                    "source_versions",
                )
            }
            if 'body_reference' in note:
                metadata['body_reference'] = note['body_reference']
        elif events:
            event = events[0]
            body = event["payload"]
            metadata = {k: event[k] for k in ("session_id", "seq", "run_id", "created")}
            if view == 'content':
                message = json.loads(body)
                if event['kind'] != 'message' or message.get('role') != 'user':
                    raise ValueError('content 视图只读取用户消息原文')
                body = content_text(message.get('content'))
                metadata.update(role='user', view='content', total_chars=len(body))
        elif files:
            row = files[0]
            body = row["body"]
            current = self.database.rows(
                "SELECT digest FROM files f WHERE workspace_id=? AND path=? "
                "AND NOT EXISTS (SELECT 1 FROM index_dirty d WHERE d.workspace_id=f.workspace_id AND d.path=f.path)",
                (self.workspace_id, row["path"]),
            )
            metadata = {
                "kind": "code",
                "historical_snapshot": True,
                "path": row["path"],
                "sha256": row["digest"],
                "current": bool(current and current[0]["digest"] == row["digest"]),
            }
        else:
            raise ValueError("引用不存在或不属于当前工作区")
        if revision is not None and not notes:
            raise ValueError("Only knowledge entries have revision numbers")
        if view == 'content' and not events:
            raise ValueError('content 视图只读取用户消息原文')
        def page(count):
            return {**metadata, 'ref': ref, 'text': body[offset:offset + count], 'offset': offset,
                    'next_offset': offset + count if len(body) > offset + count else None,
                    'total_chars': len(body)}
        count = min(limit, max(0, len(body) - offset))
        if page_tokens is not None:
            # The page is JSON inside tool-message JSON. Count both escaping
            # layers and reserve the tool-result envelope; offsets count source
            # characters, never escaped wire bytes.
            def cost(size):
                return (len(encode(encode(page(size)))) + 2) // 3 + 256
            if cost(min(1, count)) > page_tokens:
                raise ValueError('当前分页预算无法容纳引用与元数据')
            low, high = min(1, count), count
            while low < high:
                middle = (low + high + 1) // 2
                if cost(middle) <= page_tokens:
                    low = middle
                else:
                    high = middle - 1
            count = low
        return page(count)

    def write_notes(self, entries):
        from genesisai.core.memory.note_writes import write
        return write(self, entries)

    def prepare_notes(self, entries):
        from genesisai.core.memory.note_writes import prepare_batch
        return prepare_batch(self, entries)

    def commit_notes(self, prepared):
        from genesisai.core.memory.note_writes import commit
        return commit(self, prepared)

    def notes(self, *, offset=0, limit=None):
        if type(offset) is not int or offset < 0 or (limit is not None and (type(limit) is not int or limit < 1)):
            raise ValueError('Invalid note range')
        return self.database.rows(
            "SELECT id,title,kind,status,revision,updated FROM notes WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL) ORDER BY updated DESC,id LIMIT ? OFFSET ?",
            (self.project_id, self.workspace_id, limit if limit is not None else -1, offset),
        )

    def delete_note(self, ref, expected_revision):
        with self.database.transaction() as db:
            invalidate_sources(db, self.project_id, [ref])
            count = db.execute(
                "DELETE FROM notes WHERE id=? AND project_id=? AND (workspace_id=? OR workspace_id IS NULL) AND revision=?",
                (ref, self.project_id, self.workspace_id, expected_revision),
            ).rowcount
            if not count:
                raise ValueError("记忆不存在或版本已变化")
            db.execute("DELETE FROM search_documents WHERE id=?", (ref,))
            db.execute("DELETE FROM search_fts WHERE id=?", (ref,))

    def export(self, destination):
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("导出目标必须是新文件")
        rows = self.database.rows(
            "SELECT * FROM notes WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
            (self.project_id, self.workspace_id),
        )
        body = "\n\n".join(
            f"## {r['title']}\n\n{r['body']}\n\n状态：{r['status']} · {r['id']} · v{r['revision']}\n来源：{r['sources']}"
            for r in rows
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as output:
            output.write(body or "# 尚无项目记忆\n")
        return str(destination)

    def rebuild_search(self):
        # Rebuild both derived tables from authoritative events, notes and file snapshots.
        from genesisai.shared.run_control import cancellation_requested
        from genesisai.shared.security import ToolError
        def check_cancelled():
            if cancellation_requested():
                raise ToolError('cancelled', '记忆搜索维护已取消；派生索引事务已回滚')
        check_cancelled()
        with self.database.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM search_documents WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
                    (self.project_id, self.workspace_id),
                )
            ]
            for identity in ids:
                check_cancelled()
                db.execute("DELETE FROM search_fts WHERE id=?", (identity,))
                db.execute("DELETE FROM search_documents WHERE id=?", (identity,))
            events = db.execute(
                "SELECT e.*,s.project_id,s.workspace_id FROM events e JOIN sessions s ON s.id=e.session_id WHERE s.project_id=? AND s.workspace_id=? AND e.kind='message'",
                (self.project_id, self.workspace_id),
            ).fetchall()
            for event in events:
                check_cancelled()
                message = json.loads(event["payload"])
                Database.index(
                    db,
                    event["id"],
                    self.project_id,
                    self.workspace_id,
                    "history",
                    message["role"],
                    content_text(message.get("content"), images=True)
                    + "\n"
                    + encode(message.get("tool_calls") or []),
                )
            for row in db.execute(
                "SELECT * FROM notes WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL) AND status IN ('active','unverified')",
                (self.project_id, self.workspace_id),
            ).fetchall():
                check_cancelled()
                Database.index(
                    db,
                    row["id"],
                    self.project_id,
                    row["workspace_id"],
                    "note",
                    row["title"],
                    row["body"],
                )
            for row in db.execute(
                "SELECT s.*,f.payload AS structure FROM file_snapshots s JOIN files f ON s.workspace_id=f.workspace_id AND s.path=f.path AND s.digest=f.digest "
                "WHERE s.workspace_id=? AND NOT EXISTS (SELECT 1 FROM index_dirty d WHERE d.workspace_id=f.workspace_id AND d.path=f.path)",
                (self.workspace_id,),
            ).fetchall():
                check_cancelled()
                Database.index(
                    db,
                    row["id"],
                    self.project_id,
                    self.workspace_id,
                    "code",
                    row["path"],
                    row["structure"] + "\n" + row["body"],
                )
            check_cancelled()
        return {
            "documents": len(
                self.database.rows(
                    "SELECT id FROM search_documents WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
                    (self.project_id, self.workspace_id),
                )
            )
        }
