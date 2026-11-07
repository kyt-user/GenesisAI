from genesisai.core.extensions.tools.shell.project_detection import inspect_project
from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        path = context.access.workspace_path(args.get('cwd', '.'), must_exist=True, allow_directory=True)
        if not path.is_dir():
            raise ToolError('invalid_path', 'cwd 必须是项目目录')
        return {'target': str(path)}

    def execute(self, context, args):
        root = context.access.workspace_path(args.get('cwd', '.'), must_exist=True, allow_directory=True)
        return inspect_project(root, **{key: args[key] for key in ('section', 'offset', 'limit') if key in args})
