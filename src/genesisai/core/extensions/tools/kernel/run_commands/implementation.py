"""Run an explicit command or a project-declared verification entry as a durable job."""

from pathlib import Path
import re

from genesisai.shared.security import ToolError
from genesisai.shared.run_control import cancellation_requested
from genesisai.core.extensions.tools.shell.commands import resolve_command
from genesisai.core.extensions.tools.shell.project_detection import automatic_project_command


def _test_entrypoint(command):
    executable = Path(command[0]).name.casefold().removesuffix(".exe").removesuffix(".cmd")
    args = command[1:]
    if any(word in {"--help", "-h", "--version", "-V", "--collect-only", "--co", "--fixtures",
                    "--listTests", "--showConfig", "--no-run", "--list"} for word in args):
        return None
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable) and args[:1] == ["-m"]:
        executable, args = (args[1], args[2:]) if len(args) > 1 else ("", [])
    if executable in {"pytest", "unittest"}:
        return executable
    if executable in {'pnpm', 'yarn', 'bun'} and args[:1] == ['test']:
        return 'npm'
    if executable in {"npm", "cargo", "go", "dotnet", "mvn", "gradle"} and args[:1] == ["test"]:
        return executable
    return None


class Implementation:
    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
        if payload.get("verification") and type(payload.get("passed")) is bool:
            return [Observation("verification", passed=payload["passed"], detail=payload["verification"] + ": " + str(payload.get("command", "")))]
        if payload.get("passed") and not payload.get("summary", {}).get("passed"):
            return []
        if 'summary' in payload and type(payload.get('passed')) is bool:
            return [Observation('verification', passed=payload['passed'], detail=str(payload.get('command', '')))]
        return []

    def _root(self, context, args, *, must_exist=True):
        root = context.access.workspace_path(args.get("cwd", "."), must_exist=must_exist, allow_directory=True)
        if not root.is_dir():
            raise ToolError("invalid_path", "cwd 必须是目录")
        return root

    def _resolve(self, context, args):
        root = self._root(context, args)
        command = args.get("command")
        detection = None
        if command is None:
            command, detection = automatic_project_command(root)
            if not command:
                raise ToolError("test_command_unknown", (detection or {}).get("selection_issue") or "测试入口不存在、不唯一或探测未完成；用 inspect_project 查看子包、声明和扫描错误，再显式提供 cwd/command")
        return resolve_command(command, root), root, args.get("timeout_seconds"), detection

    def _write_paths(self, context, args, root):
        return [str(context.access.workspace_path(path, allow_directory=True)) for path in args["write_paths"]] if "write_paths" in args else [str(root)]

    def prepare(self, context, args):
        from genesisai.shared.process_tree import environment_preview
        from genesisai.core.jobs.input_channel import interaction
        command, root, timeout, detection = self._resolve(context, args)
        return {"name": "run_commands", "command": command,
                **environment_preview(args.get('env_from', {})),
                "background": args.get("background", False),
                "verification": args.get("verification"),
                "write_paths": self._write_paths(context, args, root),
                "interaction": interaction(args.get("interaction")),
                "target": str(root), "timeout_seconds": timeout, "detected": detection}

    def execute(self, context, args):
        command, root, timeout, detection = self._resolve(context, args)
        is_test = detection is not None or _test_entrypoint(command) is not None
        framework = (detection or {}).get("test_framework") or _test_entrypoint(command)
        task = context.runtime.tasks.active_task() or {}
        job = context.runtime.jobs.start(command, str(root), timeout, framework=framework if is_test else None,
                                         task_id=task.get("id"), env_from=args.get('env_from', {}),
                                         interaction=args.get('interaction'), write_paths=self._write_paths(context, args, root),
                                         verification=args.get('verification'))
        if not args.get("background"):
            job = context.runtime.jobs.wait(job["job_id"], 5, cancel_on_interrupt=True, emit_output=True)
            if cancellation_requested():
                raise ToolError('command_cancelled', f"命令已启动后取消，可能已产生部分修改，请检查作业 {job['job_id']} 的输出。")
        if job["status"] in {"queued", "running", "stopping"}:
            context.store.record.run_runtime["background_jobs"] = True
            if not args.get("background"):
                state = context.store.record.run_runtime
                state["pending_execution_jobs"] = [*state.get("pending_execution_jobs", []), job["job_id"]]
            if is_test or args.get("verification"):
                state = context.store.record.run_runtime
                state["pending_verification_jobs"] = [*state.get("pending_verification_jobs", []), job["job_id"]]
                state["verification_required"] = True
            return job
        result = context.runtime.jobs.result(job["job_id"])
        if is_test:
            result.update(
                framework=framework,
                selected_automatically=detection is not None,
                detection_reason=(detection or {}).get("test_detection_reason"),
            )
            if detection:
                result["python_project"] = {
                    key: detection.get(key)
                    for key in ("name", "interpreter", "interpreter_source", "test_file_count", "source_roots")
                }
        return result
