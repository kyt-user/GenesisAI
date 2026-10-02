from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["query"],
        query={"type": "string", "minLength": 1, "maxLength": 400},
    ),
    permission="network",
    network=True,
    max_output_chars=12000,
)

