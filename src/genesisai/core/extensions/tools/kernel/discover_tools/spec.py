from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    parameters=object_schema(
        [],
        query={"type": "string", "minLength": 1, "maxLength": 4000},
        limit={"type": "integer", "minimum": 1, "maximum": 100, "description": "分页数量，默认 20；精确名称可直接加载完整定义"},
        offset={"type": "integer", "minimum": 0},
        provider={"type": "string", "minLength": 1},
        category={"type": "string", "minLength": 1},
        source={"type": "string", "enum": ["builtin", "external"]},
        activate={"type": "boolean", "description": "默认 true；false 仅查看候选，不加载完整定义。需要使用时按名称再次发现。"},
    ),
    permission="core",
    routes=("base",),
)
