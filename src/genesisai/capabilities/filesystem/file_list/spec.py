from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
    ),
    permission="read",
    path_scope="authorized",
)

