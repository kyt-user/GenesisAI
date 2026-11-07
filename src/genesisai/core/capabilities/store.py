"""Transactional external capability revisions; no legacy format migration."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from genesisai.core.capabilities.contracts import encode, fingerprint, validate
from genesisai.core.capabilities.assets import AssetStore, verify_bundle
from genesisai.core.capabilities.files import reject_link
from genesisai.shared.ids import uid
from genesisai.shared.security import ToolError


class CapabilityStore:
    def __init__(self, root, project_id):
        self.root = Path(root).resolve() / "capabilities"
        self.project_id = project_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = AssetStore(self.root)
        self.path = self.root / "library.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, scope TEXT NOT NULL, project_id TEXT NOT NULL, revision INTEGER NOT NULL, UNIQUE(kind,name,scope,project_id))")
            db.execute("CREATE TABLE IF NOT EXISTS revisions(draft_id TEXT NOT NULL REFERENCES drafts(id), revision INTEGER NOT NULL, digest TEXT NOT NULL, package TEXT NOT NULL, report TEXT, created REAL NOT NULL, frozen INTEGER NOT NULL, PRIMARY KEY(draft_id,revision))")
            db.execute("CREATE TABLE IF NOT EXISTS registrations(draft_id TEXT PRIMARY KEY REFERENCES drafts(id), revision INTEGER NOT NULL, digest TEXT NOT NULL, report_id TEXT NOT NULL, accepted_at REAL NOT NULL, session_id TEXT NOT NULL, enabled INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL, FOREIGN KEY(capability_id,revision) REFERENCES revisions(draft_id,revision))")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, package):
        validate(package)
        identity = uid("cap")
        project = self.project_id if package["scope"] == "project" else ""
        with self.transaction() as db:
            try:
                db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,1)", (identity, package["kind"], package["name"], package["scope"], project))
            except sqlite3.IntegrityError as exc:
                raise ToolError("capability_conflict", "同类型、作用域和名称的草稿已存在，请修订已有草稿") from exc
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,NULL,?,0)", (identity, 1, fingerprint(package), encode(package), time.time()))
        return self.get(identity)

    def _row(self, db, identity):
        row = db.execute("SELECT * FROM drafts WHERE id=? AND (scope='user' OR project_id=?)", (identity, self.project_id)).fetchone()
        if row is None:
            raise ToolError("unknown_capability", "当前作用域不存在此能力草稿")
        return row

    def get(self, identity, revision=None):
        with self.transaction() as db:
            draft = self._row(db, identity)
            row = db.execute("SELECT * FROM revisions WHERE draft_id=? AND revision=?", (identity, revision or draft["revision"])).fetchone()
            if row is None:
                raise ToolError("unknown_revision", "能力修订不存在")
            registration = db.execute("SELECT * FROM registrations WHERE draft_id=?", (identity,)).fetchone()
        package = json.loads(row["package"])
        if fingerprint(package) != row["digest"]:
            raise ToolError("capability_integrity", "能力包完整性校验失败")
        return {"id": identity, "revision": row["revision"], "current_revision": draft["revision"], "digest": row["digest"],
                "package": package, "frozen": bool(row["frozen"]), "report": json.loads(row["report"]) if row["report"] else None,
                "registration": dict(registration) if registration else None}

    def revise(self, identity, expected_revision, package):
        validate(package)
        with self.transaction() as db:
            draft = self._row(db, identity)
            if draft["revision"] != expected_revision:
                raise ToolError("revision_conflict", "草稿已被修改，请重新读取最新修订")
            if any(draft[key] != package[key] for key in ("kind", "name", "scope")):
                raise ToolError("capability_invalid", "修订不能改变类型、名称或作用域；请创建独立草稿")
            revision = expected_revision + 1
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,NULL,?,0)", (identity, revision, fingerprint(package), encode(package), time.time()))
            db.execute("UPDATE drafts SET revision=? WHERE id=?", (revision, identity))
        return self.get(identity)

    def freeze(self, identity, expected_revision):
        item = self.get(identity)
        validate(item["package"], frozen=True)
        for reference in item["package"]["files"].values():
            self.assets.resolve(reference)
        for case in item["package"]["tests"]:
            for reference in case.get("fixtures", {}).values():
                self.assets.resolve(reference)
        with self.transaction() as db:
            draft = self._row(db, identity)
            if draft["revision"] != expected_revision or item["revision"] != expected_revision:
                raise ToolError("revision_conflict", "草稿已修改，请重新读取后冻结")
            db.execute("UPDATE revisions SET frozen=1 WHERE draft_id=? AND revision=?", (identity, expected_revision))
        return self.get(identity)

    def record_report(self, identity, revision, digest, report):
        with self.transaction() as db:
            draft = self._row(db, identity)
            row = db.execute("SELECT digest,frozen FROM revisions WHERE draft_id=? AND revision=?", (identity, revision)).fetchone()
            if draft["revision"] != revision or row is None or not row["frozen"] or row["digest"] != digest:
                raise ToolError("revision_conflict", "测试期间草稿已修改，结果不能用于当前版本注册")
            db.execute("UPDATE revisions SET report=? WHERE draft_id=? AND revision=?", (encode(report), identity, revision))

    def publish(self, identity, revision, digest, report_id, session_id):
        """Called only by the host acceptance service after verified user input."""
        with self.transaction() as db:
            draft = self._row(db, identity)
            row = db.execute("SELECT * FROM revisions WHERE draft_id=? AND revision=?", (identity, revision)).fetchone()
            report = json.loads(row["report"]) if row and row["report"] else {}
            if (draft["revision"] != revision or row is None or not row["frozen"] or row["digest"] != digest
                    or fingerprint(json.loads(row["package"])) != digest or not report.get("passed")
                    or report.get("id") != report_id or report.get("digest") != digest):
                raise ToolError("acceptance_stale", "必须验收当前版本对应的通过报告；修改或重新测试后请重新验收")
            conflict = db.execute("""SELECT d.id FROM drafts d
                JOIN registrations r ON r.draft_id=d.id
                JOIN revisions v ON v.draft_id=d.id AND v.revision=r.revision
                WHERE d.id<>? AND d.kind=? AND d.name=? AND (d.scope='user' OR d.project_id=?)
                  AND r.enabled=1 AND v.digest=r.digest
                  AND json_extract(v.report, '$.id')=r.report_id AND json_extract(v.report, '$.passed')=1""",
                                  (identity, draft["kind"], draft["name"], self.project_id)).fetchone()
            if conflict:
                raise ToolError("capability_conflict", "另一作用域已注册同名能力，请先停用或使用独立名称")
            db.execute("INSERT OR REPLACE INTO registrations VALUES(?,?,?,?,?,?,1)", (identity, revision, digest, report_id, time.time(), session_id))
        return self.get(identity)

    def disable(self, identity):
        with self.transaction() as db:
            self._row(db, identity)
            db.execute("UPDATE registrations SET enabled=0 WHERE draft_id=?", (identity,))
        return self.get(identity)

    def list(self):
        with self.transaction() as db:
            identities = [r[0] for r in db.execute("SELECT id FROM drafts WHERE scope='user' OR project_id=? ORDER BY kind,name,scope", (self.project_id,))]
        return [self.get(identity) for identity in identities]

    def active(self):
        values = []
        for item in self.list():
            registration = item["registration"]
            if not registration or not registration["enabled"]:
                continue
            registered = self.get(item["id"], registration["revision"])
            report = registered["report"] or {}
            if registered["digest"] != registration["digest"] or report.get("id") != registration["report_id"] or not report.get("passed"):
                continue
            values.append(registered)
        return values

    def materialize(self, item):
        """Immutable execution bundles are projections of the accepted bytes."""
        # Serialize projections across sessions so no reader sees partial writes.
        with self.transaction():
            return self._materialize(item)

    def _materialize(self, item):
        root = self.root / "bundles" / item["id"] / str(item["revision"])
        for path in (self.root, self.root / "bundles", root.parent, root):
            reject_link(path)
        if not item["frozen"]:
            raise ToolError("capability_unfrozen", "请先冻结完整草稿，再执行测试或试用")
        files = item["package"]["files"]
        if root.exists():
            verify_bundle(root, files)
        else:
            temporary = root.parent / uid("bundle")
            temporary.mkdir(parents=True)
            self.assets.materialize(files, temporary)
            temporary.rename(root)
        return root
