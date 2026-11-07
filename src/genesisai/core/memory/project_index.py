"""Incremental, application-owned project maps with registered language parsers."""

import ast
import hashlib
import json
import os
from pathlib import Path
import time
from genesisai.core.jobs.storage import Slot, SlotBusy
from genesisai.core.memory.index_scan import IndexConfig, IndexScanner, check_cancelled
from genesisai.core.persistence.database import Database, encode
from genesisai.core.memory.invalidation import invalidate_sources
from genesisai.shared.paths import is_private_app_path

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".html",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".sql",
    ".sh",
    ".ps1",
}


def is_link(path):
    # Python 3.11 on Windows does not expose Path.is_junction().
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0) & 0x400
    )


def python_structure(text):
    tree = ast.parse(text)
    symbols = []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(
                dict(
                    name=node.name,
                    kind=type(node).__name__,
                    line=node.lineno,
                    end_line=node.end_lineno,
                )
            )
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append("." * node.level + (node.module or ""))
    return {"symbols": symbols, "imports": imports, "coverage": "python_ast"}


class ProjectIndex:
    def __init__(self, session, code_scope):
        self.session = session
        self.database = session.database
        self.code_scope = code_scope
        self.parsers = {".py": python_structure}
        self._parser_versions = {'.py': 0}
        self._changed_parsers = set()
        self.scanner = IndexScanner(session)

    def register_parser(self, suffix, parser):
        if not suffix.startswith(".") or not callable(parser):
            raise ValueError("A file suffix and callable parser are required")
        self.parsers[suffix] = parser
        self._parser_versions[suffix] = self._parser_versions.get(suffix, 0) + 1
        self._changed_parsers.add(suffix)

    def mark_changed(self, paths):
        """Invalidate known facts synchronously; extraction stays in the queue."""
        root, ws = self.session.workspace, self.session.record.workspace_id
        candidates = {(root / path).resolve() for path in paths}
        names = [path.relative_to(root).as_posix() for path in candidates
                 if path.is_relative_to(root) and self.code_scope.permits(path)]
        with self.database.transaction() as db:
            for name in names:
                db.execute('INSERT INTO index_dirty VALUES(?,?,1) ON CONFLICT(workspace_id,path) DO UPDATE SET revision=revision+1', (ws, name))
                row = db.execute('SELECT digest FROM files WHERE workspace_id=? AND path=?', (ws, name)).fetchone()
                if row:
                    ref = 'file_' + hashlib.sha256((ws + name + row['digest']).encode()).hexdigest()[:24]
                    invalidate_sources(db, self.session.record.project_id, [ref])
                    db.execute('DELETE FROM search_documents WHERE id=?', (ref,))
                    db.execute('DELETE FROM search_fts WHERE id=?', (ref,))
        self.scanner.cache = None

    def _paths(self, config, *, force=False, progress=None):
        root = self.session.workspace
        git_paths = self.scanner.paths(config, force=force, progress=progress)
        if git_paths is not None:
            return [name for name in git_paths if self.code_scope.permits(root / name)]
        paths = []
        for folder, dirs, files in os.walk(root):
            self.scanner.checkpoint(progress, phase='enumerating', candidates=len(paths))
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in config.exclude_directories and d != '.git'
                and not is_link(Path(folder) / d)
                and self.code_scope.visit_directory(Path(folder) / d)
            )
            paths.extend(
                str((Path(folder) / name).relative_to(root))
                for name in sorted(files)
                if self.code_scope.permits(Path(folder) / name)
            )
        return paths

    def refresh(self, *, force=False, changed_paths=None, progress=None):
        lock_dir = self.session.root / 'maintenance' / 'workspaces'
        lock_dir.mkdir(parents=True, exist_ok=True)
        while True:
            self.scanner.checkpoint(progress, phase='waiting_workspace')
            try:
                slot = Slot(lock_dir / (self.session.record.workspace_id + '.lock'))
                break
            except SlotBusy:
                time.sleep(.05)
        try:
            return self._refresh(force=force, changed_paths=changed_paths, progress=progress)
        finally:
            slot.close()

    def _refresh(self, *, force=False, changed_paths=None, progress=None):
        config = IndexConfig.load(self.session)
        parsers, parser_versions = dict(self.parsers), dict(self._parser_versions)
        changed_parsers = set(self._changed_parsers)
        root = self.session.workspace
        ws = self.session.record.workspace_id
        project = self.session.record.project_id
        allowed, roots = self.code_scope.sql("path")
        old = {
            r["path"]: r
            for r in self.database.rows(
                f"SELECT * FROM files WHERE workspace_id=? AND ({allowed})",
                (ws, *roots),
            )
        }
        dirty = {row['path']: row['revision'] for row in self.database.rows(
            f'SELECT path,revision FROM index_dirty WHERE workspace_id=? AND ({allowed})', (ws, *roots))}

        def unchanged(db, name):
            row = db.execute('SELECT revision FROM index_dirty WHERE workspace_id=? AND path=?', (ws, name)).fetchone()
            return (row['revision'] if row else 0) == dirty.get(name, 0)

        raced = 0
        if changed_paths is None:
            paths = self._paths(config, force=force, progress=progress)
        else:
            self.scanner.cache = None
            candidates = {(root / path).resolve() for path in changed_paths}
            paths = sorted(
                path.relative_to(root).as_posix()
                for path in candidates
                if path.is_relative_to(root) and self.code_scope.permits(path)
            )
        seen = set()
        updated = 0
        ignored_paths = self.scanner.ignored(paths, config, progress=progress)
        for index, relative in enumerate(paths):
            check_cancelled()
            if index % config.progress_batch == 0:
                self.scanner.checkpoint(progress, phase='indexing', processed=index, candidates=len(paths), updated=updated)
            candidate = root / relative
            if (
                relative in ignored_paths
                or any(part in config.exclude_directories or part == '.git' for part in Path(relative).parts)
                or (
                    candidate.name.casefold().startswith(".env")
                    or candidate.suffix.lower() in {".pem", ".key", ".pfx", ".p12"}
                    or candidate.name.lower()
                    in {
                        "credentials",
                        "credentials.json",
                        "secrets.json",
                        "secrets.yaml",
                        "id_rsa",
                        "id_ed25519",
                    }
                )
            ):
                continue
            if (
                not candidate.is_file()
                or is_link(candidate)
                or not candidate.resolve().is_relative_to(root)
                or not self.code_scope.permits(candidate.resolve())
                or is_private_app_path(candidate.resolve(), self.database.root)
            ):
                continue
            name = Path(relative).as_posix()
            seen.add(name)
            chunks = []
            with candidate.open('rb') as stream:
                while chunk := stream.read(256 * 1024):
                    check_cancelled()
                    chunks.append(chunk)
            payload = b''.join(chunks)
            digest = hashlib.sha256(payload).hexdigest()
            if (
                not force
                and candidate.suffix not in changed_parsers
                and name in old
                and name not in dirty
                and old[name]["digest"] == digest
            ):
                continue
            # Preserve text even before a language parser is installed. A later
            # parser must describe the same immutable bytes returned by memory_read.
            if candidate.suffix in TEXT_SUFFIXES or candidate.suffix in parsers:
                text = payload.decode("utf-8", errors="replace")
            else:
                try:
                    text = "" if b"\0" in payload else payload.decode("utf-8")
                except UnicodeDecodeError:
                    text = ""
            structure = {"symbols": [], "imports": [], "coverage": "file"}
            if candidate.suffix in parsers:
                try:
                    structure = parsers[candidate.suffix](text)
                except (SyntaxError, ValueError) as exc:
                    structure["parse_error"] = str(exc)[:500]
            info = dict(path=name, sha256=digest, bytes=len(payload), **structure)
            ref = (
                "file_" + hashlib.sha256((ws + name + digest).encode()).hexdigest()[:24]
            )
            with self.database.transaction() as db:
                if not unchanged(db, name):
                    raced += 1
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO files VALUES(?,?,?,?)",
                    (ws, name, digest, encode(info)),
                )
                body = text
                db.execute(
                    "INSERT OR IGNORE INTO file_snapshots VALUES(?,?,?,?,?,?)",
                    (ref, project, ws, name, digest, body),
                )
                if name in old:
                    old_ref = (
                        "file_"
                        + hashlib.sha256(
                            (ws + name + old[name]["digest"]).encode()
                        ).hexdigest()[:24]
                    )
                    db.execute("DELETE FROM search_fts WHERE id=?", (old_ref,))
                    db.execute("DELETE FROM search_documents WHERE id=?", (old_ref,))
                    if old[name]["digest"] != digest:
                        invalidate_sources(db, project, [old_ref])
                Database.index(
                    db,
                    ref,
                    project,
                    ws,
                    "code",
                    name,
                    encode(info) + "\n" + body,
                )
                db.execute('DELETE FROM index_dirty WHERE workspace_id=? AND path=?', (ws, name))
            updated += 1
        removed = (old.keys() | dirty.keys()) - seen
        if changed_paths is not None:
            removed &= set(paths)
        for name in removed:
            self.scanner.checkpoint(progress, phase='removing_obsolete', processed=len(paths), updated=updated)
            with self.database.transaction() as db:
                if not unchanged(db, name):
                    raced += 1
                    continue
                if name in old:
                    ref = 'file_' + hashlib.sha256((ws + name + old[name]['digest']).encode()).hexdigest()[:24]
                    invalidate_sources(db, project, [ref])
                    db.execute('DELETE FROM search_documents WHERE id=?', (ref,))
                    db.execute('DELETE FROM search_fts WHERE id=?', (ref,))
                db.execute(
                    "DELETE FROM files WHERE workspace_id=? AND path=?", (ws, name)
                )
                db.execute('DELETE FROM index_dirty WHERE workspace_id=? AND path=?', (ws, name))
        if changed_paths is None:
            self._changed_parsers.difference_update(suffix for suffix in changed_parsers
                if self._parser_versions.get(suffix) == parser_versions.get(suffix))
        self.scanner.checkpoint(progress, phase='completed', processed=len(paths), updated=updated)
        return {
            "indexed": len(seen),
            "updated": updated,
            "partial": bool(raced),
            'changed_during_scan': raced,
            'scan': dict(self.scanner.status),
        }

    def overview(self, limit=40, *, offset=0):
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError('Invalid index range')
        allowed, roots = self.code_scope.sql("path")
        rows = self.database.rows(
            f"SELECT payload FROM files WHERE workspace_id=? AND ({allowed}) ORDER BY path LIMIT ? OFFSET ?",
            (self.session.record.workspace_id, *roots, limit, offset),
        )
        result = [json.loads(row['payload']) for row in rows]
        for item in result:
            item['ref'] = 'file_' + hashlib.sha256((self.session.record.workspace_id + item['path'] + item['sha256']).encode()).hexdigest()[:24]
        return result
