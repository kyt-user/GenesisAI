"""Application locations shared by model configuration and runtime layers."""

import os
from pathlib import Path

WORKSPACE_DIR = "workspace"


def application_home() -> Path:
    return (
        Path(os.environ.get("GENESISAI_HOME") or Path.home() / ".genesisai")
        .expanduser()
        .resolve()
    )


def default_workspace(app_root: Path | None = None) -> Path:
    workspace = (app_root or application_home()) / WORKSPACE_DIR
    if workspace.resolve() != workspace:
        raise ValueError("默认工作区不能通过链接指向其他目录")
    return workspace


def is_private_app_path(path: Path, app_root: Path) -> bool:
    """Both paths must be resolved. Only the task workspace is public."""
    return path.is_relative_to(app_root) and not path.is_relative_to(app_root / WORKSPACE_DIR)
