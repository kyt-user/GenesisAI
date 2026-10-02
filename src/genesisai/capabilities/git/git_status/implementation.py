"""读取工作区 Git 状态，不执行任何 Git 写操作。"""

from genesisai.capabilities.git.shared import run_git
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return run_git(context, ['status', '--short', '--branch'], args.get('cwd', '.'))

