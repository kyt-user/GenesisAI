"""只读 Git 子进程封装。"""

from __future__ import annotations

import re
import subprocess

from genesisai.shared.security import ToolError


SAFE_REVISION = re.compile(r"^[A-Za-z0-9_./~^@{}+-]{1,200}$")


def repository(context, value='.'):
    path = context.access.workspace_path(value, must_exist=True, allow_directory=True)
    if not path.is_dir():
        raise ToolError('invalid_path', 'Git 路径必须是目录')
    probe = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True, text=True, encoding='utf-8', errors='replace', shell=False)
    if probe.returncode != 0:
        raise ToolError('not_git_repository', '目标目录不是 Git 仓库')
    root = context.access.workspace_path(probe.stdout.strip(), must_exist=True, allow_directory=True)
    return root


def run_git(context, args, cwd='.'):
    root = repository(context, cwd)
    completed = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, encoding='utf-8', errors='replace', shell=False, timeout=30)
    if completed.returncode != 0:
        raise ToolError('git_error', (completed.stderr or completed.stdout or 'Git 读取失败')[:500])
    value = completed.stdout
    return {'repository': str(root), 'text': value[:20000], 'truncated': len(value) > 20000}


def revision(value):
    if not isinstance(value, str) or not SAFE_REVISION.fullmatch(value) or value.startswith('-'):
        raise ToolError('validation_error', 'Git revision 无效')
    return value

