"""内核工具：声明任务完成。

对齐 Cline 的 attempt_completion：模型调用本工具给出最终结论，
运行循环按普通最终回答的条件验证该结论。
"""


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return {"result": args["result"], "command": args.get("command")}