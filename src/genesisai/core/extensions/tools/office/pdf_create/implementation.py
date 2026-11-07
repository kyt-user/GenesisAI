"""创建基础分页文字 PDF 并重新打开验证。"""

from genesisai.core.extensions.tools.office.helpers import pdf_create, preview_write
class Implementation:
    def prepare(self, context, args): return preview_write(context, args, "pdf")
    def execute(self, context, args): return pdf_create(context, args)

