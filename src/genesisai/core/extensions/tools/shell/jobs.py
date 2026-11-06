"""Session-scoped background processes owned by the host across tool calls."""

import json
import os
import re
import subprocess
import threading
import time

from genesisai.shared.ids import uid
from genesisai.shared.process_tree import owned_process
from genesisai.core.extensions.tools.shell.commands import process_invocation
from genesisai.shared.security import ToolError
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.time_limits import tool_deadline


class CommandJobs:
    def __init__(self, store, clean):
        self.root = store.root / "command_logs" / store.id
        self.clean = clean
        self.active = {}
        self.lock = threading.RLock()

    def _save(self, info):
        path = self.root / (info["job_id"] + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def _info(self, job_id):
        if not re.fullmatch(r"job_[a-zA-Z0-9_-]+", job_id):
            raise ToolError("unknown_job", "作业编号无效")
        try:
            info = json.loads(
                (self.root / (job_id + ".json")).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ToolError("unknown_job", "本会话没有此作业") from exc
        if info["status"] == "running" and job_id not in self.active:
            info["status"] = "owner_unavailable"
        return info

    def start(self, command, cwd, timeout, framework=None, task_id=None):
        invocation, env = process_invocation(command, cwd)
        with self.lock:
            if len(self.active) >= 4:
                raise ToolError("job_limit", "本会话最多同时运行四个作业")
            self.root.mkdir(parents=True, exist_ok=True)
            job_id = uid("job")
            info = {
                "job_id": job_id,
                "cwd": str(cwd),
                "command": self.clean(command),
                "started": time.time(),
                "status": "running",
                "exit_code": None,
                "timeout_seconds": timeout,
                "framework": framework,
                "task_id": task_id,
            }
            owner = owned_process(
                invocation,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                process = owner.__enter__()
            except FileNotFoundError as exc:
                raise ToolError("command_not_found", "命令不存在") from exc
            stop = threading.Event()
            try:
                self._save(info)
                thread = threading.Thread(
                    target=self._monitor,
                    args=(info, process, owner, stop, timeout),
                    daemon=True,
                )
                self.active[job_id] = (stop, thread)
                thread.start()
            except BaseException:
                self.active.pop(job_id, None)
                owner.__exit__(None, None, None)
                raise
            return dict(info)

    def _capture(self, pipe, path):
        # Drop oversized lines whole so redaction never sees half a credential.
        with pipe, path.open("w", encoding="utf-8") as output:
            remaining = 256_000
            while line := pipe.readline(32768):
                if len(line) == 32768 and not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):
                        line = pipe.readline(32768)
                    line = b"[oversized line omitted]\n"
                if remaining:
                    text = self.clean(line.decode("utf-8", errors="replace"))
                    output.write(text[:remaining])
                    remaining = max(0, remaining - len(text))
                    if not remaining:
                        output.write("\n[output limit reached]\n")
                    output.flush()

    def _monitor(self, info, process, owner, stop, timeout):
        job_id = info["job_id"]
        reader = threading.Thread(
            target=self._capture,
            args=(
                os.fdopen(os.dup(process.stdout.fileno()), "rb"),
                self.root / (job_id + ".log"),
            ),
            daemon=True,
        )
        status = "failed"
        try:
            reader.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if stop.wait(0.05):
                    status = "stopped"
                    break
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    break
            else:
                status = "succeeded" if process.returncode == 0 else "failed"
        finally:
            owner.__exit__(None, None, None)
            reader.join()
            with self.lock:
                try:
                    self._save(
                        {
                            **info,
                            "status": status,
                            "finished": time.time(),
                            "exit_code": process.returncode,
                        }
                    )
                finally:
                    self.active.pop(job_id, None)

    def status(self, job_id=None):
        with self.lock:
            if job_id:
                return self._info(job_id)
            jobs = sorted(
                self.root.glob("job_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            return {
                "jobs": [self._info(p.stem) for p in jobs[:50]],
                "truncated": len(jobs) > 50,
            }

    def output(self, job_id, offset=0, limit=4000):
        info = self.status(job_id)
        path = self.root / (job_id + ".log")
        content = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        page = content[offset : offset + limit]
        result = {
            **info,
            "text": page,
            "next_offset": offset + len(page),
            "more": offset + len(page) < len(content),
        }
        if info.get("framework") and info["status"] in {"succeeded", "failed", "timed_out", "stopped"}:
            from genesisai.core.extensions.tools.shell.python_support import parse_test_summary
            summary = parse_test_summary(info["framework"], content, "", info["exit_code"])
            result.update(summary=summary, passed=info["status"] == "succeeded" and summary["passed"] > 0 and not summary["failed"] and not summary["errors"])
        return result

    def wait(self, job_id, seconds=0):
        deadline = min(time.monotonic() + seconds, tool_deadline.get() or float("inf"))
        while True:
            info = self.status(job_id)
            if info["status"] != "running" or time.monotonic() >= deadline or cancellation_requested():
                return info
            with self.lock:
                active = self.active.get(job_id)
            if not active:
                return self.status(job_id)
            active[1].join(timeout=min(0.05, max(0, deadline - time.monotonic())))

    def stop(self, job_id):
        with self.lock:
            item = self.active.get(job_id)
            if not item:
                return self._info(job_id)
            event, thread = item
            event.set()
        thread.join()
        return self.status(job_id)

    def close(self):
        with self.lock:
            jobs = list(self.active.values())
            for event, _ in jobs:
                event.set()
        for _, thread in jobs:
            thread.join()
