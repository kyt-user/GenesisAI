from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["query"],
        query={"type": "string", "maxLength": 200},
        category={"type": "string", "minLength": 1, "maxLength": 50},
        include_disabled={"type": "boolean"},
        limit={"type": "integer", "minimum": 1, "maximum": 20},
    ),
    permission="core",
    max_output_chars=8000,
)

