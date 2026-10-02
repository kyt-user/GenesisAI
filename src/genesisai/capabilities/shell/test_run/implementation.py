"""自动识别测试框架并执行，返回结构化摘要、退出码、耗时和日志引用。"""

from genesisai.shared.security import ToolError
from genesisai.capabilities.shell.process import run_process, validate_command
from genesisai.capabilities.shell.python_support import automatic_frontend_command, automatic_test_command, parse_test_summary


class Implementation:
    def _values(self, context, args):
        cwd = context.access.workspace_path(args.get("cwd", "."), must_exist=True, allow_directory=True)
        if not cwd.is_dir():
            raise ToolError("invalid_path", "cwd 必须是目录")
        command = args.get("command")
        detection = None
        if command is None:
            command, detection = automatic_test_command(cwd)
            if not command:
                command, detection = automatic_frontend_command(cwd)
            if not command and (cwd / "package.json").is_file():
                command = ["npm", "test", "--", "--runInBand"]
                detection = {"test_framework": "npm", "test_detection_reason": "发现 package.json"}
            elif not command and (cwd / "Cargo.toml").is_file():
                command = ["cargo", "test"]
                detection = {"test_framework": "cargo", "test_detection_reason": "发现 Cargo.toml"}
            elif not command:
                raise ToolError("test_command_unknown", "无法识别测试入口，请显式提供 command")
        return validate_command(command), cwd, args.get("timeout_seconds", 300), detection

    def prepare(self, context, args):
        command, cwd, timeout, detection = self._values(context, args)
        return {
            "name": "test_run", "command": command, "target": str(cwd), "timeout_seconds": timeout,
            "detected": detection,
        }

    def execute(self, context, args):
        command, cwd, timeout, detection = self._values(context, args)
        result = run_process(context, command, cwd, timeout, kind="test")
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

