from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('creative', 'creative_writing'),
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        glob={"type": "string", "minLength": 1, "maxLength": 200},
    ),
    permission="read",
    path_scope="authorized",
)
