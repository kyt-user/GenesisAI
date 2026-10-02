from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        glob={"type": "string", "minLength": 1, "maxLength": 200},
        depth={"type": "integer", "minimum": 1, "maximum": 10},
    ),
    permission="read",
    path_scope="authorized",
)
