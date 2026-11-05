from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=("jobs",),
    permission="read",
    parameters=object_schema(
        [],
        job_id={"type": "string", "maxLength": 100},
        output={"type": "boolean"},
        offset={"type": "integer", "minimum": 0},
        limit={"type": "integer", "minimum": 1, "maximum": 8000},
    ),
)
