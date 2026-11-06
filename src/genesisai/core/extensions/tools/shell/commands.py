"""Resolve one argv invocation consistently for foreground and background tools."""

import os
import shutil
import subprocess
from pathlib import Path

from genesisai.shared.process_tree import safe_environment
from genesisai.shared.security import ToolError


def validate_command(command):
    if not isinstance(command, list) or not 1 <= len(command) <= 64:
        raise ToolError("validation_error", "command 必须是非空字符串数组且不超过 64 项")
    if any(not isinstance(arg, str) or not arg or len(arg) > 4000 or "\0" in arg for arg in command):
        raise ToolError("validation_error", "command 中每项必须是非空参数字符串")
    return command


def resolve_command(command, cwd):
    command = validate_command(command)
    name = command[0]
    root = Path(cwd).resolve()
    if os.name == "nt":
        extensions = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
                      if ext.lower() in {".exe", ".com", ".bat", ".cmd"}]
        directories = [root] if Path(name).parent != Path(".") or Path(name).is_absolute() else [
            root, *(Path(p.strip('"')) for p in os.get_exec_path() if p)]
        executable = None
        for directory in directories:
            for suffix in ["", *extensions]:
                candidate = root / directory / (name + suffix)
                if candidate.is_file():
                    executable = candidate.resolve()
                    break
            if executable is not None:
                break
    else:
        target = str(root / name) if "/" in name else name
        # Relative PATH entries are resolved against the command's cwd, as
        # they are when exec searches after the child changes directory.
        search_path = os.pathsep.join(str(root / directory) for directory in os.get_exec_path())
        executable = shutil.which(target, path=search_path)
    if executable is None:
        hint = 'command[0] 是程序路径，其余每项是一个参数，例如 ["flutter", "--version"]。'
        if os.name == "nt" and name.lower() in {"echo", "dir", "copy", "cd", "set"}:
            hint = '这是 cmd 内置命令，请显式调用 ["cmd.exe", "/d", "/c", "echo hello"] 等命令解释器入口。'
        raise ToolError("command_not_found", f"未能解析程序 {name!r}，工作目录 {root}。已检查命令路径和 PATH。{hint}")
    resolved = [str(executable), *command[1:]]
    if os.name == "nt" and Path(resolved[0]).suffix.lower() in {".bat", ".cmd"}:
        if any(any(char in arg for char in '"\r\n') for arg in resolved):
            raise ToolError("validation_error", "批处理入口的参数不能包含双引号或换行，请用原生可执行文件处理此类参数。")
    return resolved


def process_invocation(command, cwd):
    resolved = resolve_command(command, cwd)
    env = safe_environment()
    if os.name != "nt" or Path(resolved[0]).suffix.lower() not in {".bat", ".cmd"}:
        return resolved, env
    # cmd expands percent variables once. Keeping each value inside quotes and
    # disabling delayed expansion preserves literal %, !, &, spaces and carets.
    # Embedded quotes/newlines cannot be represented by this batch argv contract.
    variables = []
    for index, arg in enumerate(resolved):
        key = f"GENESISAI_BATCH_ARG_{index}"
        # Preserve trailing backslashes when a batch wrapper forwards %* to a
        # native executable using the Windows C argv convention.
        trailing = len(arg) - len(arg.rstrip("\\"))
        env[key] = arg + "\\" * trailing
        variables.append(f'"%{key}%"')
    interpreter = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
    command_line = subprocess.list2cmdline([interpreter, "/d", "/s", "/v:off", "/c"])
    return command_line + ' "' + " ".join(variables) + '"', env
