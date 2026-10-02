"""将选定的内置业务工具加载到当前任务的 Active Tools。"""

class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        active = context.runtime.load_tools(args["names"], replace=args.get("replace", False))
        return {"active_tools": active, "active_count": len(active), "effective": "next_model_request"}
