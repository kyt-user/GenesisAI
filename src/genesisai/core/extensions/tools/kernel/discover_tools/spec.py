from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    parameters=object_schema(
        ["query"],
        query={"type": "string", "minLength": 1, "maxLength": 200},
        limit={"type": "integer", "minimum": 1, "maximum": 12, "description": "返回候选数量，默认 6，上限 12"},
        activate={"type": "boolean", "description": "默认 true；false 仅查看候选，不加载完整定义。需要使用时按名称再次发现。"},
    ),
    permission="core",
    routes=("base",),
)
