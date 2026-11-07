"""Cross-session hierarchical write claims; waiting never occupies an executor slot.

Claims coordinate declared resources. They are not an OS filesystem sandbox and
cannot infer a command's writes outside its declared paths.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from genesisai.core.jobs.storage import Slot, SlotBusy


def normalize(paths):
    return sorted({os.path.normcase(str(Path(path).resolve())) for path in paths})


def overlaps(left, right):
    return any(a == b or Path(a).is_relative_to(b) or Path(b).is_relative_to(a) for a in left for b in right)


def prepared_paths(preview, workspace):
    paths = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'args':
                    continue
                if key in {'target', 'path', 'source', 'destination', 'workspace'} and isinstance(child, str) and Path(child).is_absolute():
                    paths.append(child)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(preview)
    return normalize(paths or [workspace])


def claim(root, job_id, host_lock, paths):
    if not paths:
        return True
    root = Path(root) / 'jobs'
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'writes.sqlite3', timeout=5)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS claims(id TEXT PRIMARY KEY, paths TEXT NOT NULL, lease TEXT NOT NULL, active INTEGER NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        try:
            for owner, existing, lease in db.execute('SELECT id,paths,lease FROM claims WHERE active=1').fetchall():
                if owner == job_id or not overlaps(paths, json.loads(existing)):
                    continue
                try:
                    live = Slot(lease)
                except SlotBusy:
                    db.rollback()
                    return False
                else:
                    live.close()
                    db.execute('UPDATE claims SET active=0 WHERE id=?', (owner,))
            db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?,1)', (job_id, json.dumps(paths), str(host_lock)))
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise


def release(root, job_id):
    with closing(sqlite3.connect(Path(root) / 'jobs' / 'writes.sqlite3', timeout=5)) as db:
        with db:
            db.execute('UPDATE claims SET active=0 WHERE id=?', (job_id,))
