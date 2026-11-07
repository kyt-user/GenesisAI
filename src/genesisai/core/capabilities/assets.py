"""Stream capability inputs into immutable, content-addressed assets."""

import hashlib
import json
from pathlib import Path
import shutil

from genesisai.core.capabilities.files import files_under, reject_link
from genesisai.shared.ids import uid
from genesisai.shared.security import ToolError


CHUNK_BYTES = 1024 * 1024


def file_identity(path):
    for part in (Path(path).absolute(), *Path(path).absolute().parents):
        reject_link(part)
    with Path(path).open("rb") as stream:
        size, digest = 0, hashlib.sha256()
        while chunk := stream.read(CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": size}


def verify_bundle(root, files):
    """Verify all executable bytes against the frozen file manifest."""
    root = Path(root).absolute()
    try:
        for path in (root, *root.parents):
            reject_link(path)
        if not root.is_dir():
            raise ToolError('capability_integrity', '执行包目录不存在')
        actual = {path.relative_to(root).as_posix(): file_identity(path) for path in files_under(root)}
        if actual != files:
            raise ToolError('capability_integrity', '执行包已被修改；请通过修订创建新版本并重新测试')
    except OSError as exc:
        raise ToolError('capability_integrity', '执行包无法完成完整性校验，操作未执行') from exc


class AssetStore:
    """Limits are storage/import quotas, never limits on a whole capability."""

    def __init__(self, root):
        self.root = Path(root) / "assets"
        reject_link(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = {"import_max_bytes": 512 * 1024**2, "storage_max_bytes": 5 * 1024**3}
        config = Path(root) / "limits.json"
        if config.exists():
            values = json.loads(config.read_text(encoding="utf-8"))
            if (not isinstance(values, dict) or values.keys() - self.limits.keys()
                    or any(type(v) is not int or v <= 0 for v in values.values())):
                raise ToolError("capability_config", "limits.json 仅接受正整数 import_max_bytes/storage_max_bytes")
            self.limits.update(values)

    def resolve(self, reference):
        path = self.root / reference["sha256"]
        if not path.is_file() or file_identity(path) != reference:
            raise ToolError("capability_integrity", "能力资产不存在或内容与哈希不匹配")
        return path

    def import_file(self, source):
        """Caller serializes quota decisions with the capability transaction."""
        source = Path(source)
        reject_link(source)
        before = source.stat()
        quota = self.limits["import_max_bytes"]
        if not source.is_file() or before.st_size > quota:
            raise ToolError("capability_quota", f"输入须为文件且不超过导入配额 {quota} 字节")
        used = sum(path.stat().st_size for path in self.root.iterdir() if path.is_file())
        temporary = self.root / uid("import")
        try:
            digest, size = hashlib.sha256(), 0
            with source.open("rb") as incoming, temporary.open("xb") as output:
                while chunk := incoming.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > quota:
                        raise ToolError("capability_quota", "输入增长超过导入配额")
                    digest.update(chunk)
                    output.write(chunk)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ToolError("source_changed", "导入过程中源文件发生变化，请重新引用")
            reference = {"sha256": digest.hexdigest(), "size": size}
            destination = self.root / reference["sha256"]
            if destination.exists():
                self.resolve(reference)
            else:
                if used + size > self.limits["storage_max_bytes"]:
                    raise ToolError("capability_quota", "资产存储配额不足；请明确调整配额，系统不会删除现有资产")
                temporary.rename(destination)
            return reference
        finally:
            # Only this invocation's unfinished import is removed; user inputs
            # and earlier assets/revisions are never deleted by quota handling.
            if temporary.exists():
                temporary.unlink()

    def materialize(self, files, directory):
        root = Path(directory)
        for name, reference in files.items():
            source = self.resolve(reference)
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as incoming, destination.open("xb") as output:
                shutil.copyfileobj(incoming, output, CHUNK_BYTES)
