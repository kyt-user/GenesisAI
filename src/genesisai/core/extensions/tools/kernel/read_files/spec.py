from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        [],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}, "minItems": 1, "maxItems": 50},
        line_start={"type": "integer", "minimum": 1, "maximum": 1000000},
        line_end={"type": "integer", "minimum": 1, "maximum": 1000000},
        offset={"type": "integer", "minimum": 0, "maximum": 100000000},
        limit={"type": "integer", "minimum": 1, "maximum": 200000},
    ),
    permission="read",
    path_scope="authorized",
)