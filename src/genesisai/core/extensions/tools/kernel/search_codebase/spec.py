from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        query={"type": "string", "minLength": 1, "maxLength": 2000},
        mode={"type": "string", "minLength": 1, "maxLength": 20},
        regex={"type": "boolean"},
        glob={"type": "string", "minLength": 1, "maxLength": 200},
        exclude={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "maxItems": 50},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
        depth={"type": "integer", "minimum": 1, "maximum": 10},
    ),
    permission="read",
    path_scope="authorized",
)