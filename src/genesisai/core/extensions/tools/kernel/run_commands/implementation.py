"""内核工具：受控命令执行与 Python 项目检查。

吸收原 shell_run、test_run、python_project，并承载 git_* 等命令式读取。
"""

from pathlib import Path
import re

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.shell.process import run_process, validate_command
from genesisai.core.extensions.tools.shell.python_support import (
    automatic_frontend_command,
    automatic_test_command,
    inspect_python_project,
    parse_test_summary,
)


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
    if executable in {"npm", "cargo", "go", "dotnet", "mvn", "gradle"} and args[:1] == ["test"]:
        return executable
    return None


class Implementation:
    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
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
        operation = args.get("operation", "run")
        if operation not in {"inspect", "run"} or (operation == "inspect" and args.get("background")):
            raise ToolError("validation_error", "后台任务仅支持 run 操作")
        root = self._root(context, args)
        if operation == "inspect":
            return operation, None, root, args.get("timeout_seconds", 300), None
        command = args.get("command")
        detection = None
        if command is None:
            command, detection = automatic_test_command(root)
            if not command:
                command, detection = automatic_frontend_command(root)
            if not command and (root / "package.json").is_file():
                command = ["npm", "test", "--", "--runInBand"]
                detection = {"test_framework": "npm", "test_detection_reason": "发现 package.json"}
            elif not command and (root / "Cargo.toml").is_file():
                command = ["cargo", "test"]
                detection = {"test_framework": "cargo", "test_detection_reason": "发现 Cargo.toml"}
            elif not command:
                raise ToolError("test_command_unknown", "无法识别测试入口，请显式提供 command")
        return operation, validate_command(command), root, args.get("timeout_seconds", 120), detection

    def prepare(self, context, args):
        operation, command, root, timeout, detection = self._resolve(context, args)
        if operation == "inspect":
            return {"name": "run_commands", "operation": "inspect", "target": str(root)}
        return {"name": "run_commands", "operation": "run", "command": command,
                "background": args.get("background", False),
                "target": str(root), "timeout_seconds": timeout, "detected": detection}

    def execute(self, context, args):
        operation, command, root, timeout, detection = self._resolve(context, args)
        if operation == "inspect":
            return inspect_python_project(root)
        if args.get("background"):
            job = context.runtime.jobs.start(command, str(root), timeout)
            context.store.record.run_runtime["background_jobs"] = True
            return job
        is_test = detection is not None or _test_entrypoint(command) is not None
        result = run_process(context, command, root, timeout, kind="test" if is_test else "shell")
        if is_test:
            framework = (detection or {}).get("test_framework") or _test_entrypoint(command)
            result.update(
                framework=framework,
                selected_automatically=detection is not None,
                detection_reason=(detection or {}).get("test_detection_reason"),
                summary=parse_test_summary(framework, result["stdout"], result["stderr"], result["exit_code"]),
            )
            if detection:
                result["python_project"] = {
                    key: detection.get(key)
                    for key in ("name", "interpreter", "interpreter_source", "test_file_count", "source_roots")
                }
        return result
