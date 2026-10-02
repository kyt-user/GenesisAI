from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["url"],
        url={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
        limit={"type": "integer", "minimum": 1, "maximum": 12000},
    ),
    permission="network",
    network=True,
    max_output_chars=16000,
)

