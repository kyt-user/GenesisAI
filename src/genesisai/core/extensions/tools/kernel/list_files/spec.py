from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(execution='job',
    routes=('direct_answer', 'local_files', 'file_management', 'coding'),
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={"type": "integer", "minimum": 0},
        limit={"type": "integer", "minimum": 1, "maximum": 1000},
        include={"type": "array", "items": {"type": "string"}, "description": "显式包含默认排除的目录名称，例如 node_modules；仍不跟随链接"},
    ),
    permission="read",
    path_scope="authorized",
)
