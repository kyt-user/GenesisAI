from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(
    parameters=object_schema(
        ["paths"],
        paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}, "minItems": 1, "maxItems": 50},
        cwd={"type": "string", "minLength": 1, "maxLength": 2000},
    ),
    permission="write",
    path_scope="workspace",
    side_effect=True,
)
