"""Project-scoped reads, revisioned knowledge, and exact history references."""

import json
from pathlib import Path
import re
import time
from genesisai.core.persistence.database import Database, encode
from genesisai.shared.ids import uid
from genesisai.core.memory.invalidation import invalidate_sources
from genesisai.core.memory.scope import CodeScope
from genesisai.shared.messages import content_text

NOTE_KINDS = {"decision", "constraint", "experience", "architecture", "hypothesis"}
NOTE_STATES = {"active", "unverified", "stale", "superseded"}


class MemoryService:
    def __init__(self, session, code_scope=None):
        self.session = session
        self.database = session.database
        self.project_id = session.record.project_id
        self.workspace_id = session.record.workspace_id
        self.code_scope = code_scope or CodeScope(
            session.workspace, [*session.record.grants, session.record.output]
        )

    def search(self, query, *, corpus="all", limit=8):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("搜索词必须为 1–500 字符")
        if corpus not in {"all", "history", "project", "code"}:
            raise ValueError("Unknown search corpus")
        limit = max(1, min(int(limit), 20))
        scopes = "d.project_id=? AND (d.workspace_id=? OR d.workspace_id IS NULL)"
        args = [self.project_id, self.workspace_id]
        if corpus in {"all", "code"} and not self.code_scope.unrestricted:
            allowed, paths = self.code_scope.sql("d.title")
            scopes += f" AND (d.kind != 'code' OR ({allowed}))"
            args.extend(paths)
        if corpus != "all":
            scopes += " AND d.kind=?"
            args.append({"project": "note"}.get(corpus, corpus))
        terms = re.findall(r"[A-Za-z0-9_./\\-]+|[\u3400-\u9fff]+", query)[:12]
        long = [t for t in terms if len(t) >= 3]
        # Chinese queries also recall neighboring character triples, with bounded expansion.
        for term in list(long):
            if re.fullmatch(r"[\u3400-\u9fff]+", term) and len(term) > 3:
                long.extend(
                    term[i : i + 3] for i in range(0, min(len(term) - 2, 12), 2)
                )
        found = []
        if long:
            expression = " OR ".join(
                '"' + t.replace('"', '""') + '"' for t in dict.fromkeys(long)
            )
            found = self.database.rows(
                "SELECT d.*,bm25(search_fts) AS rank FROM search_fts JOIN search_documents d ON d.id=search_fts.id WHERE search_fts MATCH ? AND "
                + scopes
                + " ORDER BY rank LIMIT ?",
                (expression, *args, limit),
            )
        # Literal matching is an intentional short-token search path, including two-character Chinese.
        if len(found) < limit and terms:
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
                (*args, *values, limit),
            )
        unique = {row["id"]: row for row in found}
        results = []
        for row in list(unique.values())[:limit]:
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

    def read(self, ref, *, offset=0, limit=6000, revision=None):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError("Invalid read range")
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
        elif events:
            event = events[0]
            body = event["payload"]
            metadata = {k: event[k] for k in ("session_id", "seq", "run_id", "created")}
        elif files:
            row = files[0]
            body = row["body"]
            current = self.database.rows(
                "SELECT digest FROM files WHERE workspace_id=? AND path=?",
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
        return dict(
            ref=ref,
            text=body[offset : offset + limit],
            offset=offset,
            next_offset=offset + limit if len(body) > offset + limit else None,
            **metadata,
        )

    def write_note(
        self,
        *,
        title,
        body,
        kind,
        sources,
        ref=None,
        expected_revision=None,
        status="unverified",
        shared=False,
    ):
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or not isinstance(sources, list)
            or any(not isinstance(source, str) for source in sources)
            or type(shared) is not bool
        ):
            raise ValueError("Invalid memory field types")
        if ref is not None and (
            not isinstance(ref, str)
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise ValueError("修改知识必须提供引用和正整数版本")
        if (
            kind not in NOTE_KINDS
            or status not in NOTE_STATES
            or not title.strip()
            or not body.strip()
        ):
            raise ValueError("Invalid memory fields")
        if len(title) > 200 or len(body) > 32000 or not sources:
            raise ValueError("记忆需要来源，标题最多200字，正文最多32000字")
        if ref in sources:
            raise ValueError("记忆不能引用自身")
        if len(sources) > 20 or len(set(sources)) != len(sources):
            raise ValueError("最多20个不重复的来源")
        provenance = {source: self.read(source, limit=1) for source in sources}
        if status == "active" and any(
            item.get("status") in {"stale", "superseded"}
            or item.get("current") is False
            for item in provenance.values()
        ):
            raise ValueError("失效来源不能支持 active 知识")
        source_versions = {
            source: item.get("revision") for source, item in provenance.items()
        }
        title, body = self.session.get_logger().journal.clean([title, body])
        now = time.time()
        with self.database.transaction() as db:
            old = (
                db.execute(
                    "SELECT * FROM notes WHERE id=? AND project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
                    (ref, self.project_id, self.workspace_id),
                ).fetchone()
                if ref
                else None
            )
            for source, metadata in provenance.items():
                if metadata.get("revision") is not None:
                    current = db.execute(
                        "SELECT revision FROM notes WHERE id=?", (source,)
                    ).fetchone()
                    if not current or current[0] != metadata["revision"]:
                        raise ValueError("来源版本已变化，请重新查阅")
                elif metadata.get("kind") == "code" and status == "active":
                    current = db.execute(
                        "SELECT 1 FROM files WHERE workspace_id=? AND path=? AND digest=?",
                        (self.workspace_id, metadata["path"], metadata["sha256"]),
                    ).fetchone()
                    if not current:
                        raise ValueError("来源文件已变化，请重新查阅")
            # Sources cannot create dependency cycles, including indirect corrections.
            queue = list(sources)
            visited = set()
            while queue:
                source = queue.pop()
                if source == ref:
                    raise ValueError("记忆来源存在循环")
                if source in visited:
                    continue
                visited.add(source)
                parent = db.execute(
                    "SELECT sources FROM notes WHERE id=?", (source,)
                ).fetchone()
                if parent:
                    queue.extend(json.loads(parent["sources"]))
            if ref and (not old or old["revision"] != expected_revision):
                raise ValueError("记忆版本已变化，请重新读取")
            identity = ref or uid("note")
            revision = old["revision"] + 1 if old else 1
            workspace = (
                old["workspace_id"] if old else (None if shared else self.workspace_id)
            )
            record = dict(
                id=identity,
                project_id=self.project_id,
                workspace_id=workspace,
                kind=kind,
                title=title,
                body=body,
                status=status,
                revision=revision,
                sources=sources,
                source_versions=source_versions,
                updated=now,
            )
            if old:
                invalidate_sources(db, self.project_id, [identity])
            db.execute(
                "INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,title=excluded.title,body=excluded.body,status=excluded.status,revision=excluded.revision,sources=excluded.sources,updated=excluded.updated",
                (
                    identity,
                    self.project_id,
                    workspace,
                    kind,
                    title,
                    body,
                    status,
                    revision,
                    encode(sources),
                    now,
                ),
            )
            db.execute(
                "INSERT INTO note_versions VALUES(?,?,?)",
                (identity, revision, encode(record)),
            )
            if status in {"active", "unverified"}:
                Database.index(
                    db, identity, self.project_id, workspace, "note", title, body
                )
            else:
                db.execute("DELETE FROM search_documents WHERE id=?", (identity,))
                db.execute("DELETE FROM search_fts WHERE id=?", (identity,))
        return record

    def notes(self):
        return self.database.rows(
            "SELECT id,title,kind,status,revision,updated FROM notes WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL) ORDER BY updated DESC",
            (self.project_id, self.workspace_id),
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
        with self.database.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM search_documents WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
                    (self.project_id, self.workspace_id),
                )
            ]
            for identity in ids:
                db.execute("DELETE FROM search_fts WHERE id=?", (identity,))
                db.execute("DELETE FROM search_documents WHERE id=?", (identity,))
            events = db.execute(
                "SELECT e.*,s.project_id,s.workspace_id FROM events e JOIN sessions s ON s.id=e.session_id WHERE s.project_id=? AND s.workspace_id=? AND e.kind='message'",
                (self.project_id, self.workspace_id),
            ).fetchall()
            for event in events:
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
                "SELECT s.*,f.payload AS structure FROM file_snapshots s JOIN files f ON s.workspace_id=f.workspace_id AND s.path=f.path AND s.digest=f.digest WHERE s.workspace_id=?",
                (self.workspace_id,),
            ).fetchall():
                Database.index(
                    db,
                    row["id"],
                    self.project_id,
                    self.workspace_id,
                    "code",
                    row["path"],
                    row["structure"] + "\n" + row["body"][:128000],
                )
        return {
            "documents": len(
                self.database.rows(
                    "SELECT id FROM search_documents WHERE project_id=? AND (workspace_id=? OR workspace_id IS NULL)",
                    (self.project_id, self.workspace_id),
                )
            )
        }
