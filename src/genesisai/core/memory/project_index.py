"""Incremental, application-owned project maps with registered language parsers."""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
from genesisai.core.persistence.database import Database, encode
from genesisai.core.memory.invalidation import invalidate_sources
from genesisai.core.persistence.projects import repository_anchor, canonical

IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".genesis",
    ".genesisai",
}
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
        self._changed_parsers = set()

    def register_parser(self, suffix, parser):
        if not suffix.startswith(".") or not callable(parser):
            raise ValueError("A file suffix and callable parser are required")
        self.parsers[suffix] = parser
        self._changed_parsers.add(suffix)

    def _paths(self, max_files):
        root = self.session.workspace
        if repository_anchor(root) != canonical(root):
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
            return sorted(
                name
                for name in set(result.stdout.decode("utf-8").split("\0"))
                if name and self.code_scope.permits(root / name)
            )
        paths = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED
                and not is_link(Path(folder) / d)
                and self.code_scope.visit_directory(Path(folder) / d)
            )
            paths.extend(
                str((Path(folder) / name).relative_to(root))
                for name in sorted(files)
                if self.code_scope.permits(Path(folder) / name)
            )
            if len(paths) > max_files:
                break
        return paths

    def refresh(self, *, max_files=5000, force=False, changed_paths=None):
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
        if changed_paths is None:
            paths = self._paths(max_files)
        else:
            candidates = {(root / path).resolve() for path in changed_paths}
            paths = sorted(
                path.relative_to(root).as_posix()
                for path in candidates
                if path.is_relative_to(root) and self.code_scope.permits(path)
            )
        seen = set()
        updated = 0
        ignored_paths = set()
        if (
            changed_paths is not None
            and paths
            and repository_anchor(root) != canonical(root)
        ):
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
                input="\0".join(paths) + "\0",
                encoding="utf-8",
                capture_output=True,
                timeout=10,
            )
            if ignored.returncode not in (0, 1):
                raise ValueError("Cannot inspect Git ignore rules")
            ignored_paths = set(ignored.stdout.split("\0"))
        for relative in paths[:max_files]:
            candidate = root / relative
            if (
                relative in ignored_paths
                or any(part in IGNORED for part in Path(relative).parts)
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
                or candidate.resolve().is_relative_to(self.database.root)
            ):
                continue
            name = Path(relative).as_posix()
            if candidate.stat().st_size > 1024 * 1024:
                continue
            seen.add(name)
            payload = candidate.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if (
                not force
                and candidate.suffix not in self._changed_parsers
                and name in old
                and old[name]["digest"] == digest
            ):
                continue
            # Preserve text even before a language parser is installed. A later
            # parser must describe the same immutable bytes returned by memory_read.
            if candidate.suffix in TEXT_SUFFIXES or candidate.suffix in self.parsers:
                text = payload.decode("utf-8", errors="replace")
            else:
                try:
                    text = "" if b"\0" in payload else payload.decode("utf-8")
                except UnicodeDecodeError:
                    text = ""
            structure = {"symbols": [], "imports": [], "coverage": "file"}
            if candidate.suffix in self.parsers:
                try:
                    structure = self.parsers[candidate.suffix](text)
                except (SyntaxError, ValueError) as exc:
                    structure["parse_error"] = str(exc)[:500]
            info = dict(path=name, sha256=digest, bytes=len(payload), **structure)
            ref = (
                "file_" + hashlib.sha256((ws + name + digest).encode()).hexdigest()[:24]
            )
            with self.database.transaction() as db:
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
                    encode(info) + "\n" + body[:128000],
                )
            updated += 1
        if len(paths) <= max_files:
            removed = old.keys() - seen
            if changed_paths is not None:
                removed &= set(paths)
            for name in removed:
                ref = (
                    "file_"
                    + hashlib.sha256(
                        (ws + name + old[name]["digest"]).encode()
                    ).hexdigest()[:24]
                )
                with self.database.transaction() as db:
                    invalidate_sources(db, project, [ref])
                    db.execute(
                        "DELETE FROM files WHERE workspace_id=? AND path=?", (ws, name)
                    )
                    db.execute("DELETE FROM search_documents WHERE id=?", (ref,))
                    db.execute("DELETE FROM search_fts WHERE id=?", (ref,))
        if changed_paths is None and len(paths) <= max_files:
            self._changed_parsers.clear()
        return {
            "indexed": len(seen),
            "updated": updated,
            "partial": len(paths) > max_files,
            "limit": max_files,
        }

    def overview(self, limit=40):
        allowed, roots = self.code_scope.sql("path")
        rows = self.database.rows(
            f"SELECT payload FROM files WHERE workspace_id=? AND ({allowed}) ORDER BY path LIMIT ?",
            (self.session.record.workspace_id, *roots, limit),
        )
        return [json.loads(row["payload"]) for row in rows]
