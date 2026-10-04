"""创建基础 PPTX 标题与项目符号版式并验证。"""

from genesisai.core.extensions.tools.office.helpers import pptx_create
class Implementation:
    def prepare(self, context, args): return {"name": "pptx_create", "target": str(context.access.write(args["path"]))}
    def execute(self, context, args): return pptx_create(context, args)

