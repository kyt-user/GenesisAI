"""读取 PPTX 幻灯片顺序、文字、备注和对象数量。"""

from genesisai.core.extensions.tools.office.helpers import pptx_read
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return pptx_read(context, args["path"])

