from genesisai.core.extensions.tools.media.helpers import probe

class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return probe(context, args)
