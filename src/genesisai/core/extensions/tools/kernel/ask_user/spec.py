from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=(
        "coding",
        "local_files",
        "direct_answer",
        "debugging",
        "revision",
        "testing",
    ),
    permission="core",
    awaits_input=True,
    parameters=object_schema(
        ["question"],
        question={"type": "string", "minLength": 1, "maxLength": 1500},
        options={
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string", "minLength": 1, "maxLength": 250},
        },
    ),
)
