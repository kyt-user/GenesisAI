from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema([], cwd={"type": "string", "minLength": 1, "maxLength": 2000}),
    permission="read",
    path_scope="workspace",
    side_effect=False,
    timeout_seconds=30,
    max_output_chars=16000,
)
