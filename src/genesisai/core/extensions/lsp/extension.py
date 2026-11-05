import json
from pathlib import Path

from genesisai.core.extensions.tools.filesystem.shared import (
    MAX_BYTES,
    read_text_document,
)
from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError

from .client import locations, query


class LanguageQuery:
    def __init__(self, servers):
        self.servers = servers

    def prepare(self, context, args):
        self.path = context.access.workspace_path(args["path"], must_exist=True)
        if not self.path.is_relative_to(context.access.workspace):
            raise ToolError("invalid_path", "语言查询限于当前项目")
        self.server = self.servers.get(self.path.suffix.lower())
        if self.server is None or args["operation"] not in {"definition", "references"}:
            raise ToolError("lsp_unsupported", "文件类型或查询操作不受支持")
        if self.path.stat().st_size > MAX_BYTES:
            raise ToolError("size_limit", "文件超过读取限制")
        self.text = read_text_document(self.path)[0]
        lines = self.text.replace("\r\n", "\n").split("\n")
        line, column = args["line"], args["column"]
        if (
            line > len(lines)
            or column > 1 + len(lines[line - 1].encode("utf-16-le")) // 2
        ):
            raise ToolError("invalid_position", "查询位置超过文档边界")
        return {
            "target": str(self.path),
            "command": self.server["command"],
            "language": self.server["language"],
            "operation": args["operation"],
            "sha256": sha(self.path),
        }

    def execute(self, context, args):
        result = query(
            self.server["command"],
            context.access.workspace,
            self.path,
            self.server["language"],
            self.text,
            args["operation"],
            args["line"],
            args["column"],
        )
        return locations(result, context.access)


def register_lsp(project_dir, registry, loader):
    path = Path(project_dir) / "lsp.json"
    if not path.is_file():
        return
    try:
        servers = json.loads(path.read_text(encoding="utf-8"))["servers"]
        if not isinstance(servers, dict):
            raise ValueError("servers 须为对象")
        if not servers:
            return
        for suffix, server in servers.items():
            command = server["command"]
            if (
                not suffix.startswith(".")
                or suffix != suffix.lower()
                or not isinstance(command, list)
                or not command
                or not all(isinstance(part, str) and part for part in command)
                or not isinstance(server["language"], str)
                or not server["language"]
            ):
                raise ValueError("每个后缀须配置 language 和非空 command 数组")
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError(f"{path} 配置无效：{exc}") from exc
    spec = ToolSpec(
        permission="read",
        launches_service=True,
        path_scope="workspace",
        routes=("coding", "debugging"),
        timeout_seconds=45,
        parameters=object_schema(
            ["operation", "path", "line", "column"],
            operation={"type": "string", "enum": ["definition", "references"]},
            path={"type": "string", "minLength": 1, "maxLength": 2000},
            line={"type": "integer", "minimum": 1},
            column={"type": "integer", "minimum": 1},
        ),
    )
    name = "lsp_query"
    registry.register(
        ToolDescriptor(
            name,
            True,
            "lsp",
            "查询定义或引用；行列从 1 开始，列按 UTF-16 计数",
            Path(project_dir),
            spec,
        )
    )
    loader.register(name, LanguageQuery(servers))
