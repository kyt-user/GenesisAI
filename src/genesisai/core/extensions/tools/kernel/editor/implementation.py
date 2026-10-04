"""内核工具：单一编辑内核。吸收 file_create/file_patch/file_move/file_delete/file_copy/directory_create。"""

import hashlib
import re
import shutil
from pathlib import Path

from genesisai.shared.changes import atomic_write_bytes, backup_file, current_sha, record_change
from genesisai.shared.security import ToolError
from genesisai.core.state.store import sha
from genesisai.core.extensions.tools.filesystem.patching import apply_unified_patch
from genesisai.core.extensions.tools.filesystem.shared import MAX_BYTES, encode_text_document, read_text_document
from genesisai.core.extensions.tools.shell.static_validate import validate_html_text


REPORT_SUFFIXES = frozenset({".md", ".txt", ".csv", ".json"})
SOURCE_SUFFIXES = frozenset({
    ".toml", ".yaml", ".yml", ".xml", ".ini", ".cfg",
    ".py", ".pyi", ".java", ".kt", ".kts",
    ".html", ".htm", ".css", ".scss",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".php", ".rb", ".sql", ".sh", ".ps1", ".bat", ".cmd",
})
TEXT_SUFFIXES = REPORT_SUFFIXES | SOURCE_SUFFIXES
OPERATIONS = frozenset({"create", "patch", "move", "delete", "copy", "mkdir"})


def _require(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError("validation_error", f"operation 需要参数 {key}")
    return value


class Implementation:
    def prepare(self, context, args):
        operation = args.get("operation")
        if operation not in OPERATIONS:
            raise ToolError("validation_error", "operation 必须是 create/patch/move/delete/copy/mkdir 之一")
        return getattr(self, f"_prepare_{operation}")(context, args)

    def execute(self, context, args):
        operation = args["operation"]
        return getattr(self, f"_execute_{operation}")(context, args)

    # --- create ---
    def _prepare_create(self, context, args):
        scope = args.get("scope", "output")
        if scope not in {"output", "workspace"}:
            raise ToolError("validation_error", "scope 只能是 output 或 workspace")
        target = context.access.write(args["path"]) if scope == "output" else context.access.workspace_path(args["path"])
        if target.exists():
            raise ToolError("target_exists", "目标已存在；请使用补丁修改")
        return {"name": "editor", "operation": "create", "args": args, "target": str(target),
                "grants": context.store.data["grants"], "output": context.store.data["output"]}

    def _execute_create(self, context, args):
        refs = args.get("source_refs", [])
        suffix = Path(args["path"]).suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            raise ToolError("unsupported_format", "只支持文本、报告、配置和源码文件；不支持二进制或未知扩展名")
        is_source = suffix in SOURCE_SUFFIXES
        if is_source and refs:
            raise ToolError("invalid_source", "源码文件不能附加报告来源；请将 source_refs 设为空数组")
        if not is_source:
            embedded = set(re.findall(r"\b(src_[0-9a-f]{16})\b", args.get("content", "")))
            if not embedded.issubset(refs) or any(ref not in context.store.data["sources"] for ref in refs):
                raise ToolError("invalid_source", "报告含未登记或未声明的来源")
        body = args.get("content", "")
        if refs:
            context.store.verify_sources()
            if suffix not in {".md", ".txt"}:
                raise ToolError("invalid_source", "带来源报告请使用 Markdown 或 TXT")
            body += "\n\n## 来源\n"
            for ref in dict.fromkeys(refs):
                source = context.store.data["sources"][ref]
                body += f"- [{ref}] {source['title']} — {source.get('url', source.get('path'))}\n"
        payload = body.encode("utf-8")
        scope = args.get("scope", "output")
        if scope == "output":
            path = context.access.create(args["path"], payload)
            if path.is_relative_to(context.access.workspace):
                record_change(context.store, operation="create", path=path, after_sha=hashlib.sha256(payload).hexdigest())
            artifact_refs = [context.store.artifact(path)]
        else:
            path = context.access.workspace_path(args["path"])
            if path.exists():
                raise ToolError("target_exists", "目标已存在；请使用补丁修改")
            atomic_write_bytes(path, payload)
            record_change(context.store, operation="create", path=path, after_sha=hashlib.sha256(payload).hexdigest())
            artifact_refs = []
        validation = None
        if suffix in {".html", ".htm"}:
            validation = {"framework": "frontend_static", **validate_html_text(body, Path(args["path"]).as_posix())}
        return {
            "path": str(path),
            "kind": "source" if is_source else "report",
            "requires_verification": is_source,
            "validation": validation,
            "source_refs": refs,
            "artifact_refs": artifact_refs,
        }

    # --- patch ---
    def _patch_values(self, context, args):
        target = context.access.workspace_path(_require(args, "path"), must_exist=True)
        before = current_sha(target)
        expected = args.get("expected_sha256")
        if expected and expected.casefold() != before:
            raise ToolError("patch_conflict", "文件哈希与 expected_sha256 不一致")
        text, encoding, newline, bom = read_text_document(target)
        updated = apply_unified_patch(text, _require(args, "patch"), newline)
        payload = encode_text_document(updated, encoding, bom)
        return target, before, hashlib.sha256(payload).hexdigest(), payload

    def _prepare_patch(self, context, args):
        target, before, after, _ = self._patch_values(context, args)
        return {"name": "editor", "operation": "patch", "args": args, "target": str(target),
                "before_sha256": before, "after_sha256": after}

    def _execute_patch(self, context, args):
        target, before, _, payload = self._patch_values(context, args)
        backup = backup_file(context.store, target)
        atomic_write_bytes(target, payload)
        after = current_sha(target)
        change = record_change(context.store, operation="patch", path=target, before_sha=before, after_sha=after, backup=backup)
        return {"path": str(target), "before_sha256": before, "after_sha256": after, "change_id": change["id"]}

    # --- move ---
    def _move_values(self, context, args):
        source = context.access.workspace_path(_require(args, "source"), must_exist=True)
        target = context.access.workspace_path(_require(args, "destination"))
        if target.exists():
            raise ToolError("target_exists", "移动目标已经存在")
        digest = current_sha(source)
        if args.get("expected_sha256") and args["expected_sha256"].casefold() != digest:
            raise ToolError("file_changed", "源文件哈希已经变化")
        return source, target, digest

    def _prepare_move(self, context, args):
        source, target, digest = self._move_values(context, args)
        return {"name": "editor", "operation": "move", "args": args, "source": str(source), "target": str(target), "sha256": digest}

    def _execute_move(self, context, args):
        source, target, digest = self._move_values(context, args)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        change = record_change(context.store, operation="move", path=source, before_sha=digest, after_sha=None, destination=target)
        return {"source": str(source), "destination": str(target), "sha256": digest, "change_id": change["id"]}

    # --- delete ---
    def _delete_values(self, context, args):
        target = context.access.workspace_path(_require(args, "path"), must_exist=True)
        digest = current_sha(target)
        if args.get("expected_sha256") and args["expected_sha256"].casefold() != digest:
            raise ToolError("file_changed", "文件哈希已经变化")
        return target, digest

    def _prepare_delete(self, context, args):
        target, digest = self._delete_values(context, args)
        return {"name": "editor", "operation": "delete", "args": args, "target": str(target), "sha256": digest, "destructive": True}

    def _execute_delete(self, context, args):
        target, digest = self._delete_values(context, args)
        backup = backup_file(context.store, target)
        target.unlink()
        change = record_change(context.store, operation="delete", path=target, before_sha=digest, backup=backup)
        return {"path": str(target), "deleted": True, "change_id": change["id"]}

    # --- copy ---
    def _copy_values(self, context, args):
        target = context.access.write(_require(args, "path"))
        source = context.access.read(_require(args, "source"))
        if not source.is_file():
            raise ToolError("invalid_source", "复制来源必须是文件")
        if source.stat().st_size > MAX_BYTES:
            raise ToolError("size_limit", "复制文件超过 8 MiB")
        return target, source

    def _prepare_copy(self, context, args):
        target, source = self._copy_values(context, args)
        return {"name": "editor", "operation": "copy", "args": args, "target": str(target),
                "grants": context.store.data["grants"], "output": context.store.data["output"],
                "source_sha256": sha(source)}

    def _execute_copy(self, context, args):
        target, source = self._copy_values(context, args)
        path = context.access.create(args["path"], source.read_bytes())
        if sha(path) != sha(source):
            raise ToolError("integrity_error", "复制文件哈希不一致")
        return {"path": str(path), "artifact_refs": [context.store.artifact(path)]}

    # --- mkdir ---
    def _mkdir_target(self, context, args):
        target = context.access.workspace_path(_require(args, "path"))
        if target.exists():
            raise ToolError("target_exists", "目标路径已经存在")
        return target

    def _prepare_mkdir(self, context, args):
        target = self._mkdir_target(context, args)
        return {"name": "editor", "operation": "mkdir", "args": args, "target": str(target)}

    def _execute_mkdir(self, context, args):
        target = self._mkdir_target(context, args)
        target.mkdir(parents=True, exist_ok=False)
        change = record_change(context.store, operation="mkdir", path=target)
        return {"path": str(target), "created": True, "change_id": change["id"]}