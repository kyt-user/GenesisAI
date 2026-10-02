"""在工作区创建新的 UTF-8 文本、报告、配置或源码文件，禁止覆盖和二进制文件。"""

import hashlib
import re
from pathlib import Path

from genesisai.shared.changes import atomic_write_bytes, record_change
from genesisai.shared.security import ToolError
from genesisai.capabilities.shell.static_validate import validate_html_text


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


class Implementation:
    def prepare(self, context, args):
        scope = args.get("scope", "output")
        if scope not in {"output", "workspace"}:
            raise ToolError("validation_error", "scope 只能是 output 或 workspace")
        target = context.access.write(args["path"]) if scope == "output" else context.access.workspace_path(args["path"])
        if target.exists():
            raise ToolError("target_exists", "目标已存在；请使用补丁修改")
        return {
            "name": "file_create",
            "args": args,
            "target": str(target),
            "grants": context.store.data["grants"],
            "output": context.store.data["output"],
        }

    def execute(self, context, args):
        refs = args["source_refs"]
        suffix = Path(args["path"]).suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            raise ToolError(
                "unsupported_format",
                "只支持文本、报告、配置和源码文件；不支持二进制或未知扩展名",
            )
        is_source = suffix in SOURCE_SUFFIXES
        if is_source and refs:
            raise ToolError("invalid_source", "源码文件不能附加报告来源；请将 source_refs 设为空数组")
        if not is_source:
            embedded = set(re.findall(r"\b(src_[0-9a-f]{16})\b", args["content"]))
            if not embedded.issubset(refs) or any(ref not in context.store.data["sources"] for ref in refs):
                raise ToolError("invalid_source", "报告含未登记或未声明的来源")
        body = args["content"]
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
                record_change(
                    context.store,
                    operation="create",
                    path=path,
                    after_sha=hashlib.sha256(payload).hexdigest(),
                )
        elif scope == "workspace":
            path = context.access.workspace_path(args["path"])
            if path.exists():
                raise ToolError("target_exists", "目标已存在；请使用补丁修改")
            atomic_write_bytes(path, payload)
            record_change(context.store, operation="create", path=path, after_sha=hashlib.sha256(payload).hexdigest())
        else:
            raise ToolError("validation_error", "scope 只能是 output 或 workspace")
        artifact_refs = [context.store.artifact(path)] if scope == "output" else []
        validation = None
        if suffix in {".html", ".htm"}:
            validation = {
                "framework": "frontend_static",
                **validate_html_text(body, Path(args["path"]).as_posix()),
            }
        return {
            "path": str(path),
            "kind": "source" if is_source else "report",
            "requires_verification": is_source,
            "validation": validation,
            "source_refs": refs,
            "artifact_refs": artifact_refs,
        }

