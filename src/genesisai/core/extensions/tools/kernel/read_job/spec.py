from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=("jobs",),
    permission="read",
    timeout_seconds=35,
    parameters=object_schema(
        [],
        job_id={"type": "string", "maxLength": 100},
        input_id={"type": "string", "minLength": 1, "description": "查询交互输入的持久回执；必须同时给出 job_id"},
        output={"type": "boolean"},
        offset={"type": "integer", "minimum": 0},
        limit={"type": "integer", "minimum": 1, "maximum": 8000},
        wait_seconds={"type": "number", "minimum": 0, "maximum": 30, "description": "等待此作业完成，最多 30 秒；运行中返回状态，可继续等待"},
    ),
)
