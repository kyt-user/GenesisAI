"""读取有限数量的 Git 提交日志。"""

from genesisai.capabilities.git.shared import run_git
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return run_git(context, ['log', f"-{args.get('limit', 10)}", '--oneline', '--decorate'], args.get('cwd', '.'))

