"""内核工具：声明任务完成。

对齐 Cline 的 attempt_completion：模型调用本工具给出最终结论，
运行循环以该结论收敛为 completed。
"""


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return {"completed": True, "result": args["result"], "command": args.get("command")}