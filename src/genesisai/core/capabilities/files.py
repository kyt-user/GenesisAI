"""Traverse execution files without following symlinks or Windows junctions."""

import os
from pathlib import Path

from genesisai.shared.security import ToolError


def reject_link(path):
    if path.is_symlink() or (path.exists() and bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)):
        raise ToolError("capability_integrity", "能力目录与产物不能包含链接：" + str(path))


def files_under(root):
    root = Path(root)
    reject_link(root)
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted([*folders, *files]):
            path = Path(directory) / name
            reject_link(path)
        for name in sorted(files):
            yield Path(directory) / name
