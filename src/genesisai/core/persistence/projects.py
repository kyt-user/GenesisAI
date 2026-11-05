"""Stable local project identities and worktree bindings."""

import os
from pathlib import Path
import subprocess
import time
from genesisai.shared.ids import uid


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def repository_anchor(workspace):
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
        )
        if result.returncode == 0:
            ignored = subprocess.run(
                ["git", "-C", str(workspace), "check-ignore", "-q", "."],
                capture_output=True,
                timeout=5,
            )
            if ignored.returncode == 0:
                return canonical(workspace)
    except (OSError, subprocess.TimeoutExpired):
        return canonical(workspace)
    return (
        canonical(result.stdout.strip())
        if result.returncode == 0
        else canonical(workspace)
    )


def bind_workspace(database, workspace):
    path = canonical(workspace)
    anchor = repository_anchor(workspace)
    with database.transaction() as db:
        row = db.execute(
            "SELECT id,project_id FROM workspaces WHERE path=?", (path,)
        ).fetchone()
        if row:
            return row["project_id"], row["id"]
        project = db.execute(
            "SELECT id FROM projects WHERE anchor=?", (anchor,)
        ).fetchone()
        project_id = project["id"] if project else uid("project")
        if not project:
            db.execute(
                "INSERT INTO projects VALUES(?,?,?)", (project_id, anchor, time.time())
            )
        workspace_id = uid("workspace")
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?)", (workspace_id, project_id, path)
        )
    return project_id, workspace_id
