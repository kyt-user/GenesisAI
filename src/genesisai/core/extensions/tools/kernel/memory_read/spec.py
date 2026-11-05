from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    routes=("memory", "tool_output"),
    parameters=object_schema(
        ["ref"],
        ref={"type": "string", "minLength": 1, "maxLength": 100},
        offset={"type": "integer", "minimum": 0},
        revision={"type": "integer", "minimum": 1},
        limit={"type": "integer", "minimum": 1, "maximum": 16000},
    ),
    permission="core",
    history_lookup=True,
    side_effect=False,
    max_output_chars=24000,
)
