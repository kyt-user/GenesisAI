"""从 XLSX 原件生成指定单元格修改副本并验证。"""

from genesisai.core.extensions.tools.office.helpers import xlsx_edit, preview_write
class Implementation:
    def prepare(self, context, args): return preview_write(context, args, "xlsx")
    def execute(self, context, args): return xlsx_edit(context, args)

