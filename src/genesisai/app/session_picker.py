"""Read-only history inventory for the terminal session picker."""
import json
from datetime import datetime

from genesisai.app.select import select
from genesisai.core.state.store import validate_session


def choose_session(console, root, current_id):
    entries = []
    skipped = 0
    for path in (root / 'sessions').glob('session_*.json'):
        if path.stem == current_id:
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            validate_session(data, path.stem)
            modified = path.stat().st_mtime
            title = next((m.get('content') for m in reversed(data['messages'])
                          if m.get('role') == 'user' and m.get('content')), '空会话')
            entries.append((modified, data, ' '.join(str(title).split())[:80]))
        except (OSError, ValueError, TypeError):
            skipped += 1
    if skipped:
        console.print(f'有 {skipped} 个会话文件无法读取，已跳过。')
    if not entries:
        console.print('没有其他可选择的历史会话。')
        return None
    entries.sort(key=lambda entry: entry[0], reverse=True)
    options = [(title, f"{datetime.fromtimestamp(modified):%m-%d %H:%M} · {data['id']} · {data['workspace']} · {data['status']}")
               for modified, data, title in entries]
    index = select(console, '选择历史会话', options, searchable=True)
    return entries[index][1]['id'] if index is not None else None
