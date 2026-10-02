"""读取指定 Git revision 的提交或文件内容。"""

from genesisai.capabilities.git.shared import revision, run_git
class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args): return run_git(context, ['show', '--stat', '--oneline', revision(args['revision'])], args.get('cwd', '.'))

