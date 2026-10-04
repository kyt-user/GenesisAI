"""创建基础 XLSX 并验证工作表和值与公式结构。"""

from genesisai.core.extensions.tools.office.helpers import xlsx_create
class Implementation:
    def prepare(self, context, args): return {"name": "xlsx_create", "target": str(context.access.write(args["path"]))}
    def execute(self, context, args): return xlsx_create(context, args)

