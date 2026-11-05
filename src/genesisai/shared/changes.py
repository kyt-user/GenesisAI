"""记录工作区文件变化并保存写入前的备份。"""

from __future__ import annotations

import os
import shutil
from tempfile import NamedTemporaryFile
from pathlib import Path

from genesisai.shared.ids import sha, uid


def backup_file(store, path):
    path = Path(path)
    if not path.is_file():
        return None
    root = store.root / 'backups' / (getattr(store.record, 'run_id') or 'no_run')
    root.mkdir(parents=True, exist_ok=True)
    target = root / (uid('backup') + path.suffix)
    shutil.copy2(path, target)
    return str(target)


def record_change(store, *, operation, path, before_sha=None, after_sha=None, backup=None, destination=None):
    item = {'id': uid('change'), 'run_id': getattr(store.record, 'run_id'), 'operation': operation, 'path': str(Path(path).resolve()), 'before_sha256': before_sha, 'after_sha256': after_sha, 'backup': backup, 'destination': str(Path(destination).resolve()) if destination else None}
    store.record.changes.append(item)
    store.commit()
    return item


def atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=".genesis-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        if path.exists():
            shutil.copymode(path, temp)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def current_sha(path):
    path = Path(path)
    return sha(path) if path.is_file() else None
