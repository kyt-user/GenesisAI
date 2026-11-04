"""Read-only history inventory for the terminal session picker."""
import json
from datetime import datetime
from pathlib import Path

from genesisai.app.select import select
from genesisai.core.state.store import validate_session


def session_entries(console, root, *, current_id=None, workspace=None):
    entries = []
    skipped = 0
    for path in (root / 'sessions').glob('session_*.json'):
        if path.stem == current_id:
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            validate_session(data, path.stem)
            modified = path.stat().st_mtime
            if workspace is not None and Path(data['workspace']) != workspace:
                continue
            title = next((m.get('content') for m in data['messages']
                          if m.get('role') == 'user' and (m.get('content') or '').strip()), None)
            if not title:
                continue
            entries.append((modified, data['id'], ' '.join(title.split())[:80], data['workspace']))
        except (OSError, ValueError, TypeError, AttributeError):
            skipped += 1
    if skipped:
        console.print(f'有 {skipped} 个会话文件无法读取，已跳过。')
    entries.sort(key=lambda entry: entry[0], reverse=True)
    return [(session_id, title, f"{datetime.fromtimestamp(modified):%m-%d %H:%M} · {folder}")
            for modified, session_id, title, folder in entries]


def choose_session(console, root, current_id):
    entries = session_entries(console, root, current_id=current_id)
    if not entries:
        console.print('没有其他可选择的历史会话。')
        return None
    options = [(title, detail + ' · ' + session_id) for session_id, title, detail in entries]
    index = select(console, '选择历史会话', options, searchable=True)
    return entries[index][0] if index is not None else None
