from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    routes=('memory',),
    parameters=object_schema(
        ["query"],
        query={"type": "string", "minLength": 1, "maxLength": 4000},
        corpus={"type": "string", "enum": ["all", "history", "project", "code"]},
        limit={"type": "integer", "minimum": 1, "maximum": 100},
        offset={"type": "integer", "minimum": 0},
    ),
    permission="core",
    history_lookup=True,
    side_effect=False,
    max_output_chars=24000,
)
