"""内核工具：单一编辑内核。吸收 file_create/file_patch/file_move/file_delete/file_copy/directory_create。"""

import hashlib
import os
import re
import shutil
from pathlib import Path

from genesisai.shared.changes import atomic_write_bytes, atomic_write_stream, backup_file, current_sha, record_change
from genesisai.core.tools.base import PartialToolResult
from genesisai.shared.security import ToolError
from genesisai.shared.ids import sha

from genesisai.core.extensions.tools.filesystem.staged_edit import stage
from genesisai.core.extensions.tools.filesystem.shared import read_text_document
from genesisai.core.extensions.tools.shell.static_validate import validate_html_text


REPORT_SUFFIXES = frozenset({".md", ".rst", ".adoc", ".txt", ".csv", ".json"})
BINARY_SUFFIXES = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".zip", ".gz", ".7z", ".rar", ".tar",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
    ".mp3", ".mp4", ".wav", ".ogg", ".mov", ".avi", ".woff", ".woff2", ".ttf",
})
OPERATIONS = frozenset({"create", "replace", "patch", "move", "delete", "copy", "mkdir"})


def _require(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError("validation_error", f"operation 需要参数 {key}")
    return value


class Implementation:
    @staticmethod
    def _text(context, args, key, *, required=True):
        reference = args.get(key + '_ref')
        if reference is not None:
            if key in args:
                raise ToolError('validation_error', f'{key} 与 {key}_ref 只能提供一个')
            path = context.access.read(reference['path'])
            if sha(path) != reference['sha256'].casefold():
                raise ToolError('file_changed', f'{key}_ref 文件与指定哈希不一致')
            value = read_text_document(path)[0]
            if sha(path) != reference['sha256'].casefold():
                raise ToolError('file_changed', f'{key}_ref 在读取期间发生变化')
        else:
            value = args.get(key)
        if value is None or (required and not value):
            raise ToolError('validation_error', f'需要 {key} 或 {key}_ref')
        return value

    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
        from pathlib import Path
        from genesisai.shared.ids import sha
        paths = [payload.get('path'), payload.get('source'), payload.get('destination')]
        paths.extend(item["path"] for item in payload.get("applied", []))
        observations = [Observation('resource_changed', path=str(Path(p).resolve()),
                        sha256=sha(Path(p)) if Path(p).is_file() else None) for p in dict.fromkeys(paths) if p]
        validation = payload.get('validation')
        if isinstance(validation, dict) and type(validation.get('passed')) is bool:
            observations.append(Observation('verification', passed=validation['passed'], detail='static validation'))
        return observations

    def prepare(self, context, args):
        operation = args.get("operation")
        if operation not in OPERATIONS:
            raise ToolError("validation_error", "operation 必须是 create/replace/patch/move/delete/copy/mkdir 之一")
        return getattr(self, f"_prepare_{operation}")(context, args)

    def execute(self, context, args):
        operation = args["operation"]
        return getattr(self, f"_execute_{operation}")(context, args)

    # --- create ---
    def _prepare_create(self, context, args):
        self._create_body(context, args)
        scope = args.get("scope", "output")
        if scope not in {"output", "workspace"}:
            raise ToolError("validation_error", "scope 只能是 output 或 workspace")
        target = context.access.write(args["path"]) if scope == "output" else context.access.workspace_path(args["path"])
        if target.exists():
            raise ToolError("target_exists", "目标已存在；请使用补丁修改")
        return {'name': 'editor', 'operation': 'create', 'args': args, 'target': str(target), 'grants': context.store.record.grants, 'output': context.store.record.output}

    def _create_body(self, context, args):
        body = self._text(context, args, 'content', required=False)
        refs = args.get("source_refs", [])
        suffix = Path(args["path"]).suffix.lower()
        is_source = suffix not in REPORT_SUFFIXES
        if suffix in BINARY_SUFFIXES or "\0" in body:
            raise ToolError("unsupported_format", "editor 创建文本；二进制格式请使用文件引用或专用处理器")
        if any(ref not in context.store.record.sources for ref in refs):
            raise ToolError("invalid_source", "产物来源必须是已登记引用")
        if not is_source:
            embedded = set(re.findall(r"\b(src_[0-9a-f]{16})\b", body))
            if not embedded.issubset(refs) or any((ref not in context.store.record.sources for ref in refs)):
                raise ToolError("invalid_source", "报告含未登记或未声明的来源")
        if refs:
            context.store.verify_sources()
        return body, refs, suffix, is_source

    def _execute_create(self, context, args):
        body, refs, suffix, is_source = self._create_body(context, args)
        payload = body.encode("utf-8")
        scope = args.get("scope", "output")
        if scope == "output":
            path = context.access.create(args["path"], payload)
            if context.access.host_access or path.is_relative_to(context.access.workspace):
                record_change(context.store, operation="create", path=path, after_sha=hashlib.sha256(payload).hexdigest())
            artifact_refs = [context.store.artifact(path, refs)]
        else:
            path = context.access.workspace_path(args["path"])
            if path.exists():
                raise ToolError("target_exists", "目标已存在；请使用补丁修改")
            atomic_write_bytes(path, payload)
            record_change(context.store, operation="create", path=path, after_sha=hashlib.sha256(payload).hexdigest())
            artifact_refs = [context.store.artifact(path, refs)] if refs else []
        validation = None
        if suffix in {".html", ".htm"}:
            validation = {"framework": "frontend_static", **validate_html_text(body, Path(args["path"]).as_posix())}
        return {
            "path": str(path),
            "created": True,
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
        if args.get("operation") == "replace":
            payload, after, diff = stage(target, operation='replace',
                old_text=self._text(context, args, 'old_text'),
                new_text=self._text(context, args, 'new_text', required=False))
        else:
            payload, after, diff = stage(target, operation='patch', patch=self._text(context, args, 'patch'))
        if current_sha(target) != before:
            payload.close()
            raise ToolError('patch_conflict', '预检期间文件已改变')
        return target, before, after, payload, diff

    def _prepare_patch(self, context, args):
        batch = args.get("patches")
        if batch is not None and (args["operation"] != "patch" or any(key in args for key in ("patch", "patch_ref", "path", "expected_sha256"))):
            raise ToolError("validation_error", "批量补丁请仅提供 patches")
        self.changes = [self._patch_values(context, entry) for entry in (batch or [args])]
        if len({change[0] for change in self.changes}) != len(self.changes):
            raise ToolError("validation_error", "批量补丁的目标文件重复")
        previews = [{"target": str(path), "before_sha256": before, "after_sha256": after,
                     "diff": diff[:3000], "diff_truncated": len(diff) > 3000}
                    for path, before, after, _, diff in self.changes]
        return {"name": "editor", "operation": args["operation"], "args": args,
                **({"files": previews} if batch else previews[0])}

    def _execute_patch(self, context, args):
        applied = []
        for i, (path, before, after, payload, _) in enumerate(self.changes):
            written = False
            try:
                if current_sha(path) != before:
                    raise ToolError("patch_conflict", "预览后文件已改变")
                backup = backup_file(context.store, path)
                payload.seek(0)
                atomic_write_stream(path, payload)
                payload.close()
                written = True
                change = record_change(context.store, operation="patch", path=path, before_sha=before,
                                       after_sha=after, backup=backup)
                applied.append({"path": str(path), "before_sha256": before, "after_sha256": after,
                                "change_id": change["id"]})
            except Exception as exc:
                if "patches" not in args:
                    raise
                if written:
                    applied.append({"path": str(path), "before_sha256": before, "after_sha256": after,
                                    "backup": backup, "change_id": None})
                return PartialToolResult("batch_incomplete", str(exc), applied=applied, failed_path=str(path),
                                         unattempted=[str(item[0]) for item in self.changes[i+1:]])
        return {"applied": applied} if "patches" in args else applied[0]

    _prepare_replace = _prepare_patch
    _execute_replace = _execute_patch

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
        return target, source

    def _prepare_copy(self, context, args):
        target, source = self._copy_values(context, args)
        return {'name': 'editor', 'operation': 'copy', 'args': args, 'target': str(target), 'grants': context.store.record.grants, 'output': context.store.record.output, 'source_sha256': sha(source)}

    def _execute_copy(self, context, args):
        target, source = self._copy_values(context, args)
        target.parent.mkdir(parents=True, exist_ok=True)
        path = context.access.write(args["path"])
        with source.open("rb") as incoming, path.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
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
