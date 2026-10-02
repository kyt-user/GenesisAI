"""Git commit — 提交暂存区内容。"""

from genesisai.capabilities.git.shared import repository


class Implementation:
    def prepare(self, context, args):
        return {"name": "git_commit", "target": f"git commit -m \"{args.get('message', '')[:60]}\""}

    def execute(self, context, args):
        import subprocess
        message = args.get("message", "")
        cwd = args.get("cwd", ".")
        root = repository(context, cwd)
        completed = subprocess.run(
            ["git", "-C", str(root), "commit", "-m", message],
            capture_output=True, text=True, encoding="utf-8", errors="replace", shell=False, timeout=30,
        )
        if completed.returncode != 0:
            from genesisai.shared.security import ToolError
            raise ToolError("git_error", (completed.stderr or "git commit 失败")[:500])
        return {
            "repository": str(root),
            "message": message,
            "text": (completed.stdout or "")[:5000],
        }
