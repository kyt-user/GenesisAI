"""查看一个内置工具的参数、权限、限制和当前状态。"""

class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return context.runtime.catalog.describe(args["name"])
