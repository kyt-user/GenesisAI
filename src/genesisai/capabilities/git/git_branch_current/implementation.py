"""读取当前 Git 分支名称或 detached HEAD 状态。"""

from genesisai.capabilities.git.shared import run_git
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return run_git(context, ['branch', '--show-current'], args.get('cwd', '.'))

