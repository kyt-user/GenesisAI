from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["steps", "acceptance"],
        steps={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500}, "minItems": 1, "maxItems": 30},
        acceptance={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500}, "minItems": 1, "maxItems": 30},
        non_goals={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500}, "maxItems": 20},
    ),
    permission="write",
    path_scope="workspace",
    side_effect=True,
    max_output_chars=8000,
)