from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        [],
        query={"type": "string", "minLength": 1, "maxLength": 500},
        url={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={"type": "integer", "minimum": 0, "maximum": 100000000},
        limit={"type": "integer", "minimum": 1, "maximum": 200000},
    ),
    permission="network",
    network=True,
    path_scope="none",
)