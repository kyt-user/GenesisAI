"""读取 DOCX 的段落、样式、表格和页眉页脚结构。"""

from genesisai.core.extensions.tools.office.helpers import docx_read
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return docx_read(context, args["path"])

