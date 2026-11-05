from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=("jobs",),
    permission="shell",
    side_effect=True,
    parameters=object_schema(
        ["job_id"], job_id={"type": "string", "minLength": 1, "maxLength": 100}
    ),
)
