from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    parameters=object_schema(
        ["query"],
        query={"type": "string", "minLength": 1, "maxLength": 500},
        corpus={"type": "string", "enum": ["all", "history", "project", "code"]},
        limit={"type": "integer", "minimum": 1, "maximum": 20},
    ),
    permission="core",
    history_lookup=True,
    side_effect=False,
    max_output_chars=24000,
)
