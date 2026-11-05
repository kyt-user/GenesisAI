"""Durable cooperative maintenance queue, independent of model/tool execution."""

import json
import time
from genesisai.core.persistence.database import encode
from genesisai.shared.ids import uid


class Maintenance:
    def __init__(self, session):
        self.session = session
        self.handlers = {}

    def register(self, name, handler):
        if name in self.handlers or not callable(handler):
            raise ValueError("Maintenance handler requires a unique name and callable")
        self.handlers[name] = handler

    def enqueue(self, kind, payload=None):
        if kind not in self.handlers:
            raise ValueError("Unregistered maintenance job: " + kind)
        identity = uid("job")
        with self.session.database.transaction() as db:
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    self.session.record.workspace_id,
                    kind,
                    "queued",
                    encode({"input": payload or {}}),
                    time.time(),
                ),
            )
        return identity

    def run_pending(self, *, limit=1):
        completed = []
        for _ in range(limit):
            with self.session.database.transaction() as db:
                row = db.execute(
                    "SELECT * FROM jobs WHERE workspace_id=? AND state='queued' ORDER BY updated LIMIT 1",
                    (self.session.record.workspace_id,),
                ).fetchone()
                if row is None:
                    break
                job = dict(row)
                db.execute(
                    "UPDATE jobs SET state='running',updated=? WHERE id=?",
                    (time.time(), job["id"]),
                )
            payload = json.loads(job["payload"])
            try:
                payload["result"] = self.handlers[job["kind"]](payload["input"])
                state = "completed"
            except Exception as exc:
                state = "failed"
                payload["error"] = f"{type(exc).__name__}: {exc}"
                self.session.get_logger().error(
                    "memory_maintenance_failed", exc, job_id=job["id"]
                )
            with self.session.database.transaction() as db:
                db.execute(
                    "UPDATE jobs SET state=?,payload=?,updated=? WHERE id=?",
                    (state, encode(payload), time.time(), job["id"]),
                )
            completed.append({"id": job["id"], "state": state, **payload})
        return completed

    @staticmethod
    def recover_after_host_lock(database):
        """Only the exclusive application host may reclaim interrupted derived work."""
        with database.transaction() as db:
            db.execute(
                "UPDATE jobs SET state='queued',updated=? WHERE state='running'",
                (time.time(),),
            )
