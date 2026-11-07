"""按章节块输出有覆盖范围的大纲与事实候选。"""
from genesisai.core.extensions.tools.novel.facts import extract_block
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return extract_block(context, args)
