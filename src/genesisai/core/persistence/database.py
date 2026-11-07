"""One transactional authority; FTS and code indexes are rebuildable projections."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from genesisai.core.paths import ApplicationPaths

SCHEMA_VERSION = 1
INDEX_DIRTY_SCHEMA = 'CREATE TABLE IF NOT EXISTS index_dirty(workspace_id TEXT NOT NULL, path TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(workspace_id,path))'
SCHEMA = """
CREATE TABLE projects(id TEXT PRIMARY KEY, anchor TEXT UNIQUE NOT NULL, created REAL NOT NULL);
CREATE TABLE workspaces(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), path TEXT UNIQUE NOT NULL);
CREATE TABLE sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), workspace_id TEXT NOT NULL REFERENCES workspaces(id), revision INTEGER NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE events(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, seq INTEGER NOT NULL, run_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, UNIQUE(session_id,seq));
CREATE INDEX events_session_kind ON events(session_id,kind,seq);
CREATE TABLE calls(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id,id));
CREATE TABLE resources(session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, id TEXT NOT NULL, collection TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id,id));
CREATE TABLE notes(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), workspace_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, sources TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE note_versions(note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE, revision INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(note_id,revision));
CREATE TABLE tasks(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, payload TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE files(workspace_id TEXT NOT NULL REFERENCES workspaces(id), path TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(workspace_id,path));
CREATE TABLE file_snapshots(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE context_manifests(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE search_documents(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workspace_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
CREATE VIRTUAL TABLE search_fts USING fts5(id UNINDEXED, title, body, tokenize='trigram');
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Database:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = ApplicationPaths(self.root).database
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Opening an initialized store must not queue behind a long derived-data
        # writer: status readers and cross-instance cancellation need read access.
        with self.transaction(readonly=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(f"Unsupported memory database version: {version}")
            initialized = version == SCHEMA_VERSION and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_dirty'").fetchone()
        if initialized:
            return
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # Initialization is serialized separately from normal short transactions.
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ValueError(f"Unsupported memory database version: {version}")
            # New rebuildable coordination state, never a migration of history.
            db.execute(INDEX_DIRTY_SCHEMA)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # A single short writer transaction; no dependence on the host's WAL version.
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def transaction(self, *, readonly=False):
        db = self.connect()
        try:
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        db = self.connect()
        try:
            return [dict(row) for row in db.execute(sql, args)]
        finally:
            db.close()

    @staticmethod
    def index(db, identity, project_id, workspace_id, kind, title, body):
        db.execute("DELETE FROM search_fts WHERE id=?", (identity,))
        db.execute(
            "INSERT OR REPLACE INTO search_documents VALUES(?,?,?,?,?,?)",
            (identity, project_id, workspace_id, kind, title, body),
        )
        db.execute("INSERT INTO search_fts VALUES(?,?,?)", (identity, title, body))

    def backup(self, destination: Path):
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise ValueError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
