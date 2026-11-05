"""Limit code snapshots to the file roots selected for this session."""

from pathlib import Path


class CodeScope:
    def __init__(self, workspace, roots):
        self.workspace = Path(workspace).resolve()
        resolved = {Path(root).resolve() for root in roots if root}
        self.unrestricted = any(
            self.workspace.is_relative_to(root) for root in resolved
        )
        self.roots = tuple(
            sorted(root for root in resolved if root.is_relative_to(self.workspace))
        )

    def permits(self, path):
        return self.unrestricted or any(
            path.is_relative_to(root) for root in self.roots
        )

    def visit_directory(self, path):
        return self.permits(path) or any(
            root.is_relative_to(path) for root in self.roots
        )

    def sql(self, column):
        """Match stored workspace-relative POSIX paths before ranking or LIMIT."""
        if column not in {"path", "d.title"}:
            raise ValueError("Unsupported code path column")
        if self.unrestricted:
            return "1", ()
        paths = [root.relative_to(self.workspace).as_posix() for root in self.roots]
        predicate = f"({column} = ? OR instr({column}, ?) = 1)"
        return (
            " OR ".join(predicate for _ in paths) or "0",
            tuple(value for path in paths for value in (path, path + "/")),
        )
