"""按名称、分类和描述搜索 GenesisAI 内置工具目录。"""

class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        category = args.get("category")
        aliases = {
            "搜索": "web", "网络": "web", "网页": "web", "互联网": "web", "internet": "web",
            "文件": "filesystem", "本地": "filesystem", "本地文件": "filesystem", "local_files": "filesystem",
            "核心": "core",
            "开发": "development", "代码": "development", "测试": "development",
            "git": "git", "仓库": "git",
            "记忆": "memory", "memory": "memory",
            "办公": "office", "文档": "office", "office": "office",
        }
        category = aliases.get(str(category).casefold(), category) if category else None
        if category and category not in {"core", "filesystem", "web", "development", "git", "memory", "office"}:
            return {"tools": [], "count": 0}
        tools = context.runtime.catalog.search(
            args["query"],
            category=category,
            include_disabled=args.get("include_disabled", False),
            limit=args.get("limit", 10),
        )
        return {"tools": tools, "count": len(tools)}
