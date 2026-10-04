from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["name"],
        name={"type": "string", "minLength": 1, "maxLength": 64},
    ),
    permission="core",
    path_scope="none",
)