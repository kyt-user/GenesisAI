from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["source", "path"],
        source={"type": "string", "minLength": 1, "maxLength": 2000},
        path={"type": "string", "minLength": 1, "maxLength": 2000},
    ),
    permission="write",
    path_scope="output",
    side_effect=True,
    max_output_chars=4000,
)

