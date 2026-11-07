"""创建基础 PPTX 标题与项目符号版式并验证。"""

from genesisai.core.extensions.tools.office.helpers import pptx_create, preview_write
class Implementation:
    def prepare(self, context, args): return preview_write(context, args, "pptx")
    def execute(self, context, args): return pptx_create(context, args)

