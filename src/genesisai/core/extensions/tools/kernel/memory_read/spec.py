from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    routes=("base", "memory", "tool_output"),
    parameters=object_schema(
        ["ref"],
        ref={"type": "string", "minLength": 1, "maxLength": 100},
        view={'type': 'string', 'enum': ['record', 'content'], 'description': 'content 按字符分页读取用户消息原文；record 读取完整存档记录'},
        offset={"type": "integer", "minimum": 0},
        revision={"type": "integer", "minimum": 1},
        limit={"type": "integer", "minimum": 1, "maximum": 200000,
               "description": "本页最多读取字符数；默认按模型上下文和剩余空间动态分配，实际范围以 next_offset 为准"},
    ),
    permission="core",
    history_lookup=True,
    side_effect=False,
    max_output_chars=1500000,
)
