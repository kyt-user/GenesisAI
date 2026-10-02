"""Git add — 将文件添加到暂存区。"""

from genesisai.capabilities.git.shared import repository


class Implementation:
    def prepare(self, context, args):
        return {"name": "git_add", "target": f"git add {', '.join(args.get('paths', []))}"}

    def execute(self, context, args):
        import subprocess
        paths = args.get("paths", [])
        cwd = args.get("cwd", ".")
        root = repository(context, cwd)
        completed = subprocess.run(
            ["git", "-C", str(root), "add", "--", *paths],
            capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=30,
        )
        if completed.returncode != 0:
            from genesisai.shared.security import ToolError
            raise ToolError("git_error", (completed.stderr or "git add 失败")[:500])
        return {
            "repository": str(root),
            "added": paths,
            "text": completed.stdout[:5000] if completed.stdout else f"已添加 {len(paths)} 个路径",
        }
