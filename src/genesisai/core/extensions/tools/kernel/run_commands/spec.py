from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('local_files', 'testing', 'coding', 'git_read', 'debugging'),
    parameters=object_schema(
        [],
        operation={"type": "string", "minLength": 1, "maxLength": 20},
        command={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4000}, "minItems": 1, "maxItems": 64},
        background={"type": "boolean"},
        cwd={"type": "string", "minLength": 1, "maxLength": 2000},
        timeout_seconds={"type": "integer", "minimum": 1, "maximum": 600},
    ),
    permission="shell",
    path_scope="workspace",
    side_effect=True,
    timeout_seconds=605,
    max_output_chars=24000,
)