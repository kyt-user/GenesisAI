from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('local_files', 'coding', 'debugging'),
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        query={"type": "string", "minLength": 1, "maxLength": 2000},
        mode={"type": "string", "enum": ["name", "content", "index"]},
        output_mode={"type": "string", "enum": ["content", "files", "count"], "description": "搜索结果形式；index 使用独立文件分页"},
        cursor={"type": "string", "maxLength": 512, "description": "index 上次返回的 next_cursor；文件集变化时需重新开始"},
        max_output_chars={"type": "integer", "minimum": 1000, "maximum": 100000},
        regex={"type": "boolean"},
        glob={"type": "string", "minLength": 1, "maxLength": 200},
        exclude={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}, "maxItems": 50},
        file_offset={"type": "integer", "minimum": 0},
        context_lines={"type": "integer", "minimum": 0, "maximum": 40},
        limit={"type": "integer", "minimum": 1, "maximum": 100},
        offset={"type": "integer", "minimum": 0, "maximum": 200000},
        depth={"type": "integer", "minimum": 1, "maximum": 1000},
    ),
    permission="read",
    path_scope="authorized",
)
