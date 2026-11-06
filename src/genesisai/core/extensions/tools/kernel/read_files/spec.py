from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('direct_answer', 'local_files', 'file_management', 'coding', 'creative'),
    parameters=object_schema(
        [],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}, "minItems": 1, "maxItems": 50},
        files={"type": "array", "minItems": 1, "maxItems": 50, "description": "每个文件可指定独立范围；不与 path/paths 混用", "items": object_schema(
            ["path"], path={"type": "string", "minLength": 1, "maxLength": 2000},
            line_start={"type": "integer", "minimum": 1}, line_end={"type": "integer", "minimum": 1},
            offset={"type": "integer", "minimum": 0}, limit={"type": "integer", "minimum": 1, "maximum": 200000})},
        max_output_chars={"type": "integer", "minimum": 1000, "maximum": 200000, "description": "整次调用正文字符总量，默认 24000；返回 remaining_requests 可继续读取"},
        line_start={"type": "integer", "minimum": 1, "maximum": 1000000},
        line_end={"type": "integer", "minimum": 1, "maximum": 1000000},
        offset={"type": "integer", "minimum": 0, "maximum": 100000000},
        limit={"type": "integer", "minimum": 1, "maximum": 200000},
    ),
    permission="read",
    path_scope="authorized",
)
