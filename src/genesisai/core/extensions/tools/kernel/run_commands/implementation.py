"""内核工具：受控命令执行与 Python 项目检查。

吸收原 shell_run、test_run、python_project，并承载 git_* 等命令式读取。
"""

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.shell.process import run_process, validate_command
from genesisai.core.extensions.tools.shell.python_support import (
    automatic_frontend_command,
    automatic_test_command,
    inspect_python_project,
    parse_test_summary,
)


_TEST_HINTS = frozenset({"pytest", "nose", "unittest", "jest", "vitest", "mocha", "npm", "cargo", "go", "dotnet", "mvn", "gradle", "pytest.exe"})


def _looks_like_test(command) -> bool:
    if not command:
        return False
    names = {part.casefold() for part in command[:2]}
    if names & _TEST_HINTS:
        return True
    joined = " ".join(command).casefold()
    return "test" in joined or "pytest" in joined


class Implementation:
    def _root(self, context, args, *, must_exist=True):
        root = context.access.workspace_path(args.get("cwd", "."), must_exist=must_exist, allow_directory=True)
        if not root.is_dir():
            raise ToolError("invalid_path", "cwd 必须是目录")
        return root

    def _resolve(self, context, args):
        operation = args.get("operation", "run")
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
                "target": str(root), "timeout_seconds": timeout, "detected": detection}

    def execute(self, context, args):
        operation, command, root, timeout, detection = self._resolve(context, args)
        if operation == "inspect":
            return inspect_python_project(root)
        is_test = detection is not None or _looks_like_test(command)
        result = run_process(context, command, root, timeout, kind="test" if is_test else "shell")
        if is_test:
            framework = (detection or {}).get("test_framework", "custom")
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