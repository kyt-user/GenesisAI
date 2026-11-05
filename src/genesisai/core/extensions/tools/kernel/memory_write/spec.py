from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    parameters=object_schema(
        ["title", "body", "kind", "sources"],
        title={"type": "string", "minLength": 1, "maxLength": 200},
        body={"type": "string", "minLength": 1, "maxLength": 32000},
        kind={
            "type": "string",
            "enum": [
                "decision",
                "constraint",
                "experience",
                "architecture",
                "hypothesis",
            ],
        },
        sources={
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
        },
        ref={"type": "string", "minLength": 1},
        expected_revision={"type": "integer", "minimum": 1},
        status={
            "type": "string",
            "enum": ["active", "unverified", "stale", "superseded"],
        },
        shared={"type": "boolean"},
    ),
    permission="core",
    side_effect=True,
    max_output_chars=24000,
)
