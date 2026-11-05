"""Explicit, inspectable library maintenance commands for the terminal."""

import json
from pathlib import Path
import shlex


def execute_memory_command(runtime, command):
    parts = shlex.split(command, posix=False)
    args = [part.strip('"') for part in parts[1:]]
    memory = runtime.memory
    if not args or args == ["list"]:
        return {"notes": memory.notes()}
    if args[0] == "search" and len(args) > 1:
        return {"matches": memory.search(" ".join(args[1:]))}
    if args[0] == "read" and 2 <= len(args) <= 4:
        return memory.read(
            args[1],
            offset=int(args[2]) if len(args) >= 3 else 0,
            revision=int(args[3]) if len(args) == 4 else None,
        )
    if args[0] == "write" and len(args) == 2:
        payload = json.loads(Path(args[1]).read_text(encoding="utf-8-sig"))
        return memory.write_note(**payload)
    if args[0] == "delete" and len(args) in {3, 4}:
        ref, revision = args[1], int(args[2])
        if len(args) != 4 or args[3] != "--confirm":
            return {
                "preview": memory.read(ref),
                "scope": "仅该知识条目及其版本；关联知识将标记失效",
                "confirm": f"/memory delete {ref} {revision} --confirm",
            }
        memory.delete_note(ref, revision)
        return {"deleted": ref}
    if args[0] == "export" and len(args) == 2:
        return {"path": memory.export(args[1])}
    if args == ["rebuild"]:
        return {
            "project": runtime.project_index.refresh(force=True),
            "search": memory.rebuild_search(),
        }
    if args == ["diagnostic"]:
        return {
            "database": str(memory.database.path),
            "project_id": memory.project_id,
            "workspace_id": memory.workspace_id,
            "integrity": memory.database.rows("PRAGMA quick_check"),
            "jobs": memory.database.rows(
                "SELECT id,kind,state,payload FROM jobs WHERE workspace_id=? ORDER BY updated DESC LIMIT 10",
                (memory.workspace_id,),
            ),
            "context": runtime.store.record.run_runtime.get("context_report", {}),
        }
    if args[0] == "backup" and len(args) == 2:
        memory.database.backup(Path(args[1]))
        return {
            "database_backup": args[1],
            "assets": str(memory.database.root / "assets"),
            "note": "附件应连同 assets 目录一起备份",
        }
    raise ValueError(
        "用法：/memory list | search 关键词 | read 引用 [偏移] [版本] | write JSON路径 | delete 引用 版本 --confirm | export 新文件 | rebuild | diagnostic | backup 新文件"
    )
