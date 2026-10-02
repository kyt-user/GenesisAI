"""从 XLSX 原件生成指定单元格修改副本并验证。"""

from genesisai.capabilities.office.helpers import xlsx_edit
class Implementation:
    def prepare(self, context, args): return {"name": "xlsx_edit", "target": str(context.access.write(args["path"]))}
    def execute(self, context, args): return xlsx_edit(context, args)

