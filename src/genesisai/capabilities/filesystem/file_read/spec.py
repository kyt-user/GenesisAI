from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
        limit={"type": "integer", "minimum": 1, "maximum": 12000},
        line_start={"type": "integer", "minimum": 1, "maximum": 1000000},
        line_end={"type": "integer", "minimum": 1, "maximum": 1000000},
    ),
    permission="read",
    path_scope="authorized",
)

