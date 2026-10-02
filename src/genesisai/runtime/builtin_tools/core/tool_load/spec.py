from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["names"],
        names={
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
            "minItems": 1,
            "maxItems": 8,
        },
        replace={"type": "boolean"},
    ),
    permission="core",
    max_output_chars=4000,
)

