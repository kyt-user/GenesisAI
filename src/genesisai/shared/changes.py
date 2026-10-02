"""记录可审查、可撤销的工作区文件变化。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from genesisai.state.store import sha, uid


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


def pending_changes(store, run_id=None):
    return [item for item in store.data.get('changes', []) if not item.get('undone') and (not run_id or item.get('run_id') == run_id)]


def undo_changes(store, workspace, *, apply=False):
    """预览或按逆序安全撤销当前 Run 的文件变化。"""
    workspace = Path(workspace).resolve()
    changes = pending_changes(store, store.data.get('run_id'))
    preview = []
    for item in reversed(changes):
        path = Path(item['path']).resolve()
        target = Path(item['destination']).resolve() if item.get('destination') else path
        if not path.is_relative_to(workspace) or not target.is_relative_to(workspace):
            raise ValueError('撤销目标超出工作区')
        current = current_sha(target)
        expected = item.get('after_sha256') or (item.get('before_sha256') if item['operation'] == 'move' else None)
        conflict = expected is not None and current != expected
        preview.append({'operation': item['operation'], 'path': str(target), 'conflict': conflict})
        if apply and conflict:
            raise ValueError('文件已被外部修改，拒绝撤销：' + target.name)
    if not apply:
        return preview
    for item in reversed(changes):
        path = Path(item['path']).resolve()
        target = Path(item['destination']).resolve() if item.get('destination') else path
        operation = item['operation']
        if operation == 'create':
            target.unlink()
        elif operation in {'patch', 'delete'}:
            backup = Path(item.get('backup') or '')
            if not backup.is_file():
                raise ValueError('撤销备份缺失：' + path.name)
            atomic_write_bytes(path, backup.read_bytes())
        elif operation == 'move':
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(path))
        elif operation == 'mkdir':
            path.rmdir()
        else:
            raise ValueError('未知变更类型：' + str(operation))
        item['undone'] = True
    store.save()
    return preview

