"""Invalidate derived knowledge transitively while preserving every revision."""

import json
import time
from genesisai.core.persistence.database import encode


def invalidate_sources(db, project_id, refs):
    queue, visited = list(refs), set()
    while queue:
        source = queue.pop()
        if source in visited:
            continue
        visited.add(source)
        for row in db.execute(
            "SELECT n.sources,v.payload FROM notes n JOIN note_versions v "
            "ON v.note_id=n.id AND v.revision=n.revision "
            "WHERE n.project_id=? AND n.status IN ('active','unverified')",
            (project_id,),
        ).fetchall():
            if source not in json.loads(row["sources"]):
                continue
            note = json.loads(row["payload"])
            note.update(
                status="stale", revision=note["revision"] + 1, updated=time.time()
            )
            db.execute(
                "UPDATE notes SET status='stale',revision=?,updated=? WHERE id=?",
                (note["revision"], note["updated"], note["id"]),
            )
            db.execute(
                "INSERT INTO note_versions VALUES(?,?,?)",
                (note["id"], note["revision"], encode(note)),
            )
            db.execute("DELETE FROM search_fts WHERE id=?", (note["id"],))
            db.execute("DELETE FROM search_documents WHERE id=?", (note["id"],))
            queue.append(note["id"])
