from genesisai.core.extensions.tools.media.helpers import audio
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args):
        path = context.access.read(args['path'])
        digest = sha(path)
        if args.get('expected_sha256') not in (None, digest):
            raise ToolError('media_changed', '媒体源已变化，请重新开始')
        return {'source': str(path), 'sha256': digest, 'output': str(context.access.write(args['output']))}
    def execute(self, context, args): return audio(context, args)
