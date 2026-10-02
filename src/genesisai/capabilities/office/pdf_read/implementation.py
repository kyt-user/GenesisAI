"""读取 PDF 页数、元数据和可提取页面文字。"""

from genesisai.capabilities.office.helpers import pdf_read
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return pdf_read(context, args["path"])

