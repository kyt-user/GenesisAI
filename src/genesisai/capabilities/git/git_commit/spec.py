from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(
    parameters=object_schema(
        ["message"],
        message={"type": "string", "minLength": 1, "maxLength": 500},
        cwd={"type": "string", "minLength": 1, "maxLength": 2000},
    ),
    permission="write",
    path_scope="workspace",
    side_effect=True,
)
