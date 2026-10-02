"""检查 Python 项目结构、pyproject 元数据、测试框架和项目虚拟环境。"""

from genesisai.capabilities.shell.python_support import inspect_python_project
from genesisai.shared.security import ToolError


class Implementation:
    def _root(self, context, args):
        root = context.access.workspace_path(args.get("cwd", "."), must_exist=True, allow_directory=True)
        if not root.is_dir():
            raise ToolError("invalid_path", "cwd 必须是目录")
        return root

    def prepare(self, context, args):
        return {"name": "python_project", "target": str(self._root(context, args))}

    def execute(self, context, args):
        return inspect_python_project(self._root(context, args))
