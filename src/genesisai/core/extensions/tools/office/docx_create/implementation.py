"""创建基础 DOCX 并重新打开验证产物结构。"""

from genesisai.core.extensions.tools.office.helpers import docx_create, preview_write
class Implementation:
    def prepare(self, context, args): return preview_write(context, args, "docx")
    def execute(self, context, args): return docx_create(context, args)

