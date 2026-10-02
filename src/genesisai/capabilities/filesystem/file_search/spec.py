from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path", "query"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        query={"type": "string", "minLength": 1, "maxLength": 2000},
        content={"type": "boolean"},
        glob={"type": "string", "minLength": 1, "maxLength": 200},
        exclude={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "maxItems": 50},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
    ),
    permission="read",
    path_scope="authorized",
)

