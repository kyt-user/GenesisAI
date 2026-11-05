"""Typed session unit of work with append-only messages and optimistic commits."""

import hashlib
import json
import os
from pathlib import Path
import time

from genesisai.shared.paths import default_workspace

from genesisai.core.persistence.database import Database, encode
from genesisai.core.persistence.projects import bind_workspace
from genesisai.core.persistence.validation import validate_record
from genesisai.shared.ids import uid, sha
from genesisai.shared.messages import content_text
from genesisai.shared.session import SessionRecord


class SessionConflict(RuntimeError):
    """Another writer committed this session; reload instead of losing its changes."""


class Session:
    def __init__(self, state_root, session_id=None, *, workspace=None):
        self.root = Path(state_root).resolve()
        self.database = Database(self.root)
        self.id = session_id or uid("session")
        self.revision = 0
        self._persisted_messages = []
        if session_id:
            state, self.revision = self.inspect(self.root, session_id)
            self.record = SessionRecord(**state)
            self._binding = (
                self.record.project_id,
                self.record.workspace_id,
                self.record.workspace,
            )
            validate_record(self.record.snapshot(), self.id)
            if (
                workspace is not None
                and Path(workspace).resolve() != Path(self.record.workspace).resolve()
            ):
                raise ValueError("恢复会话的工作区与指定目录不一致")
            self._persisted_messages = [encode(m) for m in self.record.messages]
            interrupted = self.record.status == "running"
            if interrupted:
                self.record.status = "interrupted"
            for call in self.record.calls.values():
                if call["state"] == "started":
                    call["state"] = "unknown"
                    interrupted = True
            if interrupted:
                self.commit()
        else:
            workspace = Path(workspace or default_workspace(self.root)).resolve()
            project_id, workspace_id = bind_workspace(self.database, workspace)
            self.record = SessionRecord(
                self.id, str(workspace), project_id, workspace_id
            )
            self._binding = (project_id, workspace_id, str(workspace))
            self.commit()
        self.workspace = Path(self.record.workspace)

    def commit(self):
        if (
            self.record.project_id,
            self.record.workspace_id,
            self.record.workspace,
        ) != self._binding:
            raise ValueError("Session project/workspace binding cannot be changed")
        state = self.record.snapshot()
        validate_record(state, self.id)
        messages = [encode(m) for m in state.pop("messages")]
        if messages[: len(self._persisted_messages)] != self._persisted_messages:
            raise ValueError("Committed conversation events cannot be overwritten")
        calls = state.pop("calls")
        resources = {key: state.pop(key) for key in ("sources", "artifacts")}
        now = time.time()
        with self.database.transaction() as db:
            if self.revision == 0:
                db.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                    (
                        self.id,
                        self.record.project_id,
                        self.record.workspace_id,
                        1,
                        encode(state),
                        now,
                    ),
                )
            else:
                changed = db.execute(
                    "UPDATE sessions SET revision=revision+1,state=?,updated=? WHERE id=? AND revision=?",
                    (encode(state), now, self.id, self.revision),
                ).rowcount
                if not changed:
                    raise SessionConflict("会话已由另一个实例更新，请重新加载")
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=?", (self.id,)
            ).fetchone()[0]
            for payload in messages[len(self._persisted_messages) :]:
                seq += 1
                identity = uid("event")
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                    (
                        identity,
                        self.id,
                        seq,
                        self.record.run_id,
                        "message",
                        payload,
                        now,
                    ),
                )
                message = json.loads(payload)
                Database.index(
                    db,
                    identity,
                    self.record.project_id,
                    self.record.workspace_id,
                    "history",
                    message["role"],
                    content_text(message.get("content"), images=True)
                    + "\n"
                    + encode(message.get("tool_calls") or []),
                )
            for call_id, payload in calls.items():
                prior = db.execute(
                    "SELECT payload FROM calls WHERE session_id=? AND id=?",
                    (self.id, call_id),
                ).fetchone()
                serialized = encode(payload)
                if not prior or prior[0] != serialized:
                    seq += 1
                    db.execute(
                        "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                        (
                            uid("event"),
                            self.id,
                            seq,
                            self.record.run_id,
                            "tool_state",
                            serialized,
                            now,
                        ),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO calls VALUES(?,?,?)",
                        (self.id, call_id, serialized),
                    )
            for collection, items in resources.items():
                for key, value in items.items():
                    db.execute(
                        "INSERT OR REPLACE INTO resources VALUES(?,?,?,?)",
                        (self.id, key, collection, encode(value)),
                    )
        self.revision += 1
        self._persisted_messages = messages

    def history_projection(self):
        # Provenance belongs to the projection; immutable original messages stay untouched.
        messages = json.loads(json.dumps(self.record.messages))
        rows = self.database.rows(
            "SELECT id FROM events WHERE session_id=? AND kind='message' ORDER BY seq",
            (self.id,),
        )
        for message, row in zip(messages, rows):
            message.setdefault("metadata", {})
            message["metadata"] = {
                **(message.get("metadata") or {}),
                "history_ref": row["id"],
            }
        return messages

    def snapshot(self):
        return self.record.snapshot()

    @classmethod
    def inspect(cls, root, session_id):
        """Read a consistent snapshot without recovering or changing execution state."""
        database = Database(root)
        with database.transaction(readonly=True) as db:
            row = db.execute(
                "SELECT state,revision FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if not row:
                raise FileNotFoundError("Session not found: " + session_id)
            state = json.loads(row["state"])
            state["messages"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events WHERE session_id=? AND kind='message' ORDER BY seq",
                    (session_id,),
                )
            ]
            state["calls"] = {
                r["id"]: json.loads(r["payload"])
                for r in db.execute(
                    "SELECT * FROM calls WHERE session_id=?", (session_id,)
                )
            }
            for collection in ("sources", "artifacts"):
                state[collection] = {
                    r["id"]: json.loads(r["payload"])
                    for r in db.execute(
                        "SELECT * FROM resources WHERE session_id=? AND collection=?",
                        (session_id, collection),
                    )
                }
            validate_record(state, session_id)
            return state, row["revision"]

    @classmethod
    def list(cls, root, *, workspace=None):
        database = Database(root)
        query = "SELECT s.id,s.state,s.updated,w.path FROM sessions s JOIN workspaces w ON s.workspace_id=w.id"
        args = ()
        if workspace is not None:
            query += " WHERE w.path=?"
            args = (os.path.normcase(str(Path(workspace).resolve())),)
        return [
            dict(json.loads(r["state"]), updated_at=r["updated"])
            for r in database.rows(query + " ORDER BY s.updated DESC", args)
        ]

    def get_logger(self):
        if not hasattr(self, "_io_logger"):
            from genesisai.core.state.logger import RunLogger

            self._io_logger = RunLogger(self)
        return self._io_logger

    def event(self, kind, **metadata):
        allowed = {
            "call_id",
            "tool_call_id",
            "model_call_id",
            "tool",
            "ok",
            "code",
            "status",
            "elapsed_ms",
            "rounds",
            "tool_count",
            "usage",
            "profile",
            "phase",
            "finish_reason",
            "stop_reason",
            "evidence_count",
            "candidate_count",
            "retry_count",
            "prompt_names",
            "prompt_hashes",
            "diagnostic",
            "provider",
            "new_candidates",
            "repeated_candidates",
            "provider_errors",
            "context_report",
        }
        payload = dict(
            time=time.time(), session_id=self.id, run_id=self.record.run_id, event=kind
        )
        payload.update({k: v for k, v in metadata.items() if k in allowed})
        path = self.root / "traces" / f"{self.id}.jsonl"
        path.parent.mkdir(exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(self.get_logger().journal.clean(payload), ensure_ascii=False)
                + "\n"
            )

    def _file_roots(self):
        return [
            Path(p).resolve() for p in [*self.record.grants, self.record.output] if p
        ]

    def source(self, **data):
        identity = json.dumps(data, sort_keys=True, ensure_ascii=False)
        key = "src_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        if (
            data.get("kind") == "file"
            and getattr(self.record, "permission_mode") == "auto"
        ):
            path = Path(data["path"]).resolve()
            if not any(path.is_relative_to(root) for root in self._file_roots()):
                data["host_file"] = True
        self.record.sources[key] = data
        self.commit()
        return key

    def artifact(self, path):
        path = Path(path).resolve()
        digest = sha(path)
        key = (
            "artifact_" + hashlib.sha256((str(path) + digest).encode()).hexdigest()[:16]
        )
        item = dict(path=str(path), sha256=digest, run_id=self.record.run_id)
        if getattr(self.record, "permission_mode") == "auto" and (
            not path.is_relative_to(Path(self.record.output).resolve())
        ):
            item["host_file"] = True
        self.record.artifacts[key] = item
        self.commit()
        return key

    def verify_artifacts(self, *, current_run_only=False):
        latest = {data["path"]: data for data in self.record.artifacts.values()}
        changes = {item["path"]: item for item in self.record.changes}
        for data in latest.values():
            change = changes.get(data["path"])
            last_run = change.get("run_id") if change else data.get("run_id")
            if current_run_only and last_run != self.record.run_id:
                continue
            if (
                data.get("host_file")
                and getattr(self.record, "permission_mode") != "auto"
            ):
                continue
            path = Path(data["path"]).resolve()
            if not data.get("host_file") and (
                not path.is_relative_to(Path(self.record.output).resolve())
            ):
                raise ValueError("产物越界：" + path.name)
            if change and change["operation"] in {"delete", "move"}:
                continue  # 已记录的合法删除/移动使原路径退出当前产物集合，历史仍保留。
            expected = (change or {}).get("after_sha256") or data["sha256"]
            if not path.is_file() or sha(path) != expected:
                raise ValueError("产物缺失、越界或已变更：" + path.name)

    def verify_sources(self):
        self.refresh_local_files()
        for data in self.record.sources.values():
            if data["kind"] == "web":
                ref = next(
                    (
                        key
                        for key, value in self.record.sources.items()
                        if value is data
                    ),
                    None,
                )
                path = self.root / "sources" / f"{ref}.json"
                if not path.is_file():
                    raise ValueError("历史网络资料缺失，请重新执行检索")
                try:
                    page = json.loads(path.read_text(encoding="utf-8"))
                    from genesisai.shared.security import digest

                    text_digest = digest(page["text"])
                except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("历史网络资料损坏，请重新执行检索") from exc
                if text_digest != data.get("sha256"):
                    raise ValueError("历史网络资料已变更，请重新执行检索")

    def refresh_local_files(self):
        """标记已变化的本地快照；保留原始哈希和历史，不阻断继续读取。"""
        changed = []
        digests = {}
        roots = self._file_roots()
        for collection in ("sources", "artifacts"):
            for ref, data in getattr(self.record, collection).items():
                if collection == "sources" and data["kind"] != "file":
                    continue
                if (
                    data.get("host_file")
                    and getattr(self.record, "permission_mode") != "auto"
                ):
                    continue
                path = Path(data["path"]).resolve()
                if not data.get("host_file") and not any(
                    path.is_relative_to(root) for root in roots
                ):
                    raise ValueError("历史文件已不在授权范围：" + path.name)
                if path not in digests:
                    digests[path] = sha(path) if path.is_file() else None
                stale = digests[path] != data["sha256"]
                if stale != data.get("stale", False):
                    data["stale"] = stale
                    changed.append(dict(ref=ref, path=str(path), stale=stale))
        if changed:
            self.commit()
            self.get_logger().record("local_files_changed", files=changed)
