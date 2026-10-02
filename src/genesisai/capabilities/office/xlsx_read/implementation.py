"""读取 XLSX 工作表、值、公式、合并区域和结构摘要。"""

from genesisai.capabilities.office.helpers import xlsx_read
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return xlsx_read(context, args["path"])

