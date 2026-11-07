"""创建基础 XLSX 并验证工作表和值与公式结构。"""

from genesisai.core.extensions.tools.office.helpers import xlsx_create, preview_write
class Implementation:
    def prepare(self, context, args): return preview_write(context, args, "xlsx")
    def execute(self, context, args): return xlsx_create(context, args)

