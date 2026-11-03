"""记录工作区文件变化并保存写入前的备份。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from genesisai.shared.ids import sha, uid


def backup_file(store, path):
    path = Path(path)
    if not path.is_file():
        return None
    root = store.root / 'backups' / (store.data.get('run_id') or 'no_run')
    root.mkdir(parents=True, exist_ok=True)
    target = root / (uid('backup') + path.suffix)
    shutil.copy2(path, target)
    return str(target)


def record_change(store, *, operation, path, before_sha=None, after_sha=None, backup=None, destination=None):
    item = {
        'id': uid('change'),
        'run_id': store.data.get('run_id'),
        'operation': operation,
        'path': str(Path(path).resolve()),
        'before_sha256': before_sha,
        'after_sha256': after_sha,
        'backup': backup,
        'destination': str(Path(destination).resolve()) if destination else None,
    }
    store.data['changes'].append(item)
    store.save()
    return item


def atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.genesis.tmp')
    with temp.open('wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def current_sha(path):
    path = Path(path)
    return sha(path) if path.is_file() else None
