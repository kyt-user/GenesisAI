"""创建基础 DOCX 并重新打开验证产物结构。"""

from genesisai.core.extensions.tools.office.helpers import docx_create
class Implementation:
    def prepare(self, context, args): return {"name": "docx_create", "target": str(context.access.write(args["path"]))}
    def execute(self, context, args): return docx_create(context, args)

