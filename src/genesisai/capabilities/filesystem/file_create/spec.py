from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path", "content", "source_refs"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        content={"type": "string", "maxLength": 100000},
        source_refs={
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
            "maxItems": 100,
        },
        scope={"type": "string", "minLength": 6, "maxLength": 9},
    ),
    permission="write",
    path_scope="workspace",
    side_effect=True,
    max_output_chars=4000,
)

