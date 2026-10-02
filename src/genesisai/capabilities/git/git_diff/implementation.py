"""读取未暂存或已暂存的 Git diff，不修改索引和工作区。"""

from genesisai.capabilities.git.shared import run_git
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        command = ['diff'] + (['--cached'] if args.get('staged') else [])
        if args.get('paths'): command += ['--', *args['paths']]
        return run_git(context, command, args.get('cwd', '.'))

