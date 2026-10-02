"""在工作区内直接执行无 shell 插值的命令数组，记录退出码、超时和完整日志。"""

from genesisai.capabilities.shell.process import run_process, validate_command


class Implementation:
    def _values(self, context, args):
        command = validate_command(args["command"])
        cwd = context.access.workspace_path(args.get("cwd", "."), must_exist=True, allow_directory=True)
        if not cwd.is_dir():
            from genesisai.shared.security import ToolError
            raise ToolError("invalid_path", "cwd 必须是目录")
        return command, cwd, args.get("timeout_seconds", 120)

    def prepare(self, context, args):
        command, cwd, timeout = self._values(context, args)
        return {"name": "shell_run", "command": command, "target": str(cwd), "timeout_seconds": timeout}

    def execute(self, context, args):
        command, cwd, timeout = self._values(context, args)
        return run_process(context, command, cwd, timeout, kind="shell")

