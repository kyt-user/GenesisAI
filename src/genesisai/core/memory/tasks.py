"""Session-scoped task state updated from registered tool observations."""

import json
from pathlib import Path
import time
from genesisai.core.persistence.database import encode
from genesisai.shared.ids import uid, sha


class TaskService:
    def __init__(self, session):
        self.session = session
        self.database = session.database

    def active_task(self):
        rows = self.database.rows(
            "SELECT payload FROM tasks WHERE session_id=? ORDER BY updated DESC LIMIT 1",
            (self.session.id,),
        )
        return json.loads(rows[0]["payload"]) if rows else None

    def _put(self, task):
        task = self.session.get_logger().journal.clean(task)
        task["updated_at"] = time.time()
        with self.database.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO tasks VALUES(?,?,?,?)",
                (task["id"], self.session.id, encode(task), task["updated_at"]),
            )
        return task

    def begin_task(self, objective, *, run_id=None):
        task = self.active_task()
        if task and task["status"] not in {"verified", "cancelled"}:
            if run_id and run_id not in task["run_ids"]:
                task["run_ids"].append(run_id)
            return self._put(task)
        return self._put(
            dict(
                id=uid("task"),
                title=str(objective)[:200],
                status="planning",
                run_ids=[run_id] if run_id else [],
                plan_ready=False,
                verification_passed=False,
                observed_files={},
                drifted_files=[],
                last_write_at=None,
                last_verification_at=None,
                steps=[],
                acceptance=[],
                non_goals=[],
                observations=[],
                observed_calls=[],
                handoff="",
            )
        )

    def set_plan(self, *, steps, acceptance, non_goals=None):
        task = self.active_task()
        if not task:
            raise ValueError("当前没有活动开发任务")
        if not steps or not acceptance:
            raise ValueError("项目计划至少需要一个步骤和一个验收条件")
        task.update(
            steps=list(steps),
            acceptance=list(acceptance),
            non_goals=list(non_goals or []),
            plan_ready=True,
            status="planned",
        )
        return self._put(task)

    def observe(self, observations, *, call_id=None):
        task = self.active_task()
        if not task:
            return
        if call_id and call_id in task["observed_calls"]:
            return
        if call_id:
            task["observed_calls"].append(call_id)
        now = time.time()
        for item in observations:
            task["observations"].append(item)
            if item["kind"] in {"resource_read", "resource_changed"}:
                path = (self.session.workspace / item["path"]).resolve()
                if path.is_relative_to(self.session.workspace):
                    task["observed_files"][str(path)] = item.get("sha256")
            if item["kind"] == "resource_changed":
                task.update(
                    last_write_at=now, verification_passed=False, status="in_progress"
                )
            if item["kind"] == "verification":
                task.update(
                    last_verification_at=now,
                    verification_passed=item["passed"],
                    status="verified" if item["passed"] else "needs_verification",
                )
        self._put(task)

    def reconcile(self):
        task = self.active_task()
        if not task:
            return []
        drifted = [
            name
            for name, digest in task["observed_files"].items()
            if (sha(Path(name)) if Path(name).is_file() else None) != digest
        ]
        if drifted != task["drifted_files"]:
            task["drifted_files"] = drifted
            if drifted:
                task.update(verification_passed=False, status="needs_verification")
            self._put(task)
        return drifted

    def finish(self, status, answer=""):
        task = self.active_task()
        if not task or (
            self.session.record.run_id
            and self.session.record.run_id not in task["run_ids"]
        ):
            return
        if status == "cancelled":
            task["status"] = "cancelled"
        elif status == "completed":
            valid = task["verification_passed"] and (
                task["last_verification_at"] or 0
            ) >= (task["last_write_at"] or 0)
            task["status"] = "verified" if valid else "needs_verification"
        elif status == "failed":
            task["status"] = "blocked"
        task["handoff"] = answer[:4000]
        self._put(task)
        self.session.record.run_runtime.update(
            project_task_id=task["id"], project_task_status=task["status"]
        )

    def context(self):
        self.reconcile()
        task = self.active_task()
        if not task:
            return {}
        return {
            key: task[key]
            for key in (
                "id",
                "title",
                "status",
                "steps",
                "acceptance",
                "non_goals",
                "drifted_files",
                "handoff",
            )
        }
