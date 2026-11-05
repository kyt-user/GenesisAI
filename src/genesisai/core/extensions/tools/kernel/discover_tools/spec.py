from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    parameters=object_schema(
        ["query"],
        query={"type": "string", "minLength": 1, "maxLength": 200},
        limit={"type": "integer", "minimum": 1, "maximum": 4},
    ),
    permission="core",
    routes=("base",),
)
