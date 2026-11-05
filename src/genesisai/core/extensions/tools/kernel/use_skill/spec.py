from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('skill', 'local_files'),
    parameters=object_schema(
        ["name"],
        name={"type": "string", "minLength": 1, "maxLength": 64},
    ),
    permission="core",
    path_scope="none",
)