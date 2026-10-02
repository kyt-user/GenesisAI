"""在工作区内对现有文本文件应用 unified diff，并检查原文哈希和并发变化。"""

from genesisai.shared.changes import atomic_write_bytes, backup_file, current_sha, record_change
from genesisai.shared.security import ToolError
from genesisai.capabilities.filesystem.patching import apply_unified_patch
from genesisai.capabilities.filesystem.shared import encode_text_document, read_text_document


class Implementation:
    def _prepare(self, context, args):
        target = context.access.workspace_path(args["path"], must_exist=True)
        before = current_sha(target)
        expected = args.get("expected_sha256")
        if expected and expected.casefold() != before:
            raise ToolError("patch_conflict", "文件哈希与 expected_sha256 不一致")
        text, encoding, newline, bom = read_text_document(target)
        updated = apply_unified_patch(text, args["patch"], newline)
        payload = encode_text_document(updated, encoding, bom)
        import hashlib
        return target, before, hashlib.sha256(payload).hexdigest(), payload

    def prepare(self, context, args):
        target, before, after, _ = self._prepare(context, args)
        return {"name": "file_patch", "target": str(target), "before_sha256": before, "after_sha256": after}

    def execute(self, context, args):
        target, before, _, payload = self._prepare(context, args)
        backup = backup_file(context.store, target)
        atomic_write_bytes(target, payload)
        after = current_sha(target)
        change = record_change(context.store, operation="patch", path=target, before_sha=before, after_sha=after, backup=backup)
        return {"path": str(target), "before_sha256": before, "after_sha256": after, "change_id": change["id"]}

