"""Read-only history inventory; listing never recovers or mutates sessions."""

import json
import os
from datetime import datetime
from pathlib import Path
from genesisai.app.select import select
from genesisai.core.paths import ApplicationPaths
from genesisai.core.persistence.database import Database
from genesisai.shared.messages import content_text


def session_entries(console, root, *, current_id=None, workspace=None):
    if not ApplicationPaths(Path(root)).database.exists():
        return []
    database = Database(root)
    where = " WHERE s.id != ?"
    args = [current_id or ""]
    if workspace is not None:
        where += " AND w.path=?"
        args.append(os.path.normcase(str(Path(workspace).resolve())))
    rows = database.rows(
        "SELECT s.id,s.updated,w.path FROM sessions s JOIN workspaces w ON w.id=s.workspace_id"
        + where
        + " ORDER BY s.updated DESC",
        args,
    )
    entries = []
    for row in rows:
        title = None
        for event in database.rows(
            "SELECT payload FROM events WHERE session_id=? AND kind='message' ORDER BY seq",
            (row["id"],),
        ):
            message = json.loads(event["payload"])
            if message["role"] == "user":
                title = content_text(message.get("content"), images=True).strip()
                if title:
                    break
        if title:
            detail = (
                f"{datetime.fromtimestamp(row['updated']):%m-%d %H:%M} · {row['path']}"
            )
            entries.append((row["id"], " ".join(title.split())[:80], detail))
    return entries


def choose_session(console, root, current_id):
    entries = session_entries(console, root, current_id=current_id)
    if not entries:
        console.print("没有其他可选择的历史会话。")
        return None
    options = [
        (title, detail + " · " + identity) for identity, title, detail in entries
    ]
    index = select(console, "选择历史会话", options, searchable=True)
    return entries[index][0] if index is not None else None
