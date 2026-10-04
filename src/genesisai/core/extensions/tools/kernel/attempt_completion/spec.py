from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["result"],
        result={"type": "string", "minLength": 1, "maxLength": 20000},
        command={"type": "string", "minLength": 1, "maxLength": 2000},
    ),
    permission="core",
    path_scope="none",
)