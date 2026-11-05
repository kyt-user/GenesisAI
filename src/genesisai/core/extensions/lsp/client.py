"""A bounded, synchronous LSP client for one supervised query."""

import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from genesisai.shared.process_tree import owned_process, safe_environment
from genesisai.shared.security import ToolError


class Rpc:
    def __init__(self, process):
        self.process = process
        self.sequence = 0

    def send(self, **message):
        data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode(
            "utf-8"
        )
        self.process.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self.process.stdin.flush()

    def receive(self):
        length = None
        for _ in range(32):
            header = self.process.stdout.readline(2048)
            if header in (b"\r\n", b"\n"):
                break
            if not header.endswith(b"\n"):
                raise ToolError("lsp_protocol", "语言服务响应不完整")
            key, _, value = header.partition(b":")
            if key.lower() == b"content-length":
                length = int(value.strip())
        if length is None or not 0 < length <= 8 * 1024 * 1024:
            raise ToolError("lsp_protocol", "语言服务消息长度无效")
        return json.loads(self.process.stdout.read(length))

    def request(self, method, params):
        self.sequence += 1
        self.send(id=self.sequence, method=method, params=params)
        while True:
            msg = self.receive()
            if "method" in msg:
                if "id" in msg:
                    if msg["method"] == "workspace/configuration":
                        self.send(
                            id=msg["id"], result=[None] * len(msg["params"]["items"])
                        )
                    elif msg["method"] in {
                        "window/workDoneProgress/create",
                        "client/registerCapability",
                    }:
                        self.send(id=msg["id"], result=None)
                    else:
                        self.send(
                            id=msg["id"],
                            error={
                                "code": -32601,
                                "message": "Unsupported client method",
                            },
                        )
                continue
            if msg.get("id") != self.sequence or "error" in msg:
                raise ToolError(
                    "lsp_error", str(msg.get("error", "语言服务响应 ID 无效"))[:600]
                )
            return msg["result"]


def query(command, workspace, path, language, text, operation, line, column):
    root_uri = workspace.as_uri()
    document = {"uri": path.as_uri()}
    with owned_process(
        command,
        cwd=workspace,
        env=safe_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        rpc = Rpc(process)
        result = rpc.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": workspace.name}],
                "capabilities": {"general": {"positionEncodings": ["utf-16"]}},
            },
        )
        caps = result["capabilities"]
        capability = caps.get(operation + "Provider")
        if (
            capability is None
            or capability is False
            or caps.get("positionEncoding", "utf-16") != "utf-16"
        ):
            raise ToolError("lsp_unsupported", "语言服务未声明请求的能力或位置编码")
        rpc.send(method="initialized", params={})
        rpc.send(
            method="textDocument/didOpen",
            params={
                "textDocument": {
                    **document,
                    "version": 1,
                    "languageId": language,
                    "text": text,
                }
            },
        )
        params = {
            "textDocument": document,
            "position": {"line": line - 1, "character": column - 1},
        }
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        result = rpc.request("textDocument/" + operation, params)
        rpc.request("shutdown", None)
        rpc.send(method="exit", params=None)
    return result or []


def locations(result, access):
    items = []
    omitted = 0
    for entry in result if isinstance(result, list) else [result]:
        uri = urlsplit(entry.get("uri") or entry["targetUri"])
        if uri.scheme.lower() != "file" or uri.netloc not in {"", "localhost"}:
            omitted += 1
            continue
        path = Path(url2pathname(uri.path)).resolve()
        if not path.is_relative_to(access.workspace):
            omitted += 1
            continue
        try:
            path = access.workspace_path(path, must_exist=True)
        except ToolError:
            omitted += 1
            continue
        region = entry.get("range") or entry["targetSelectionRange"]
        if len(items) >= 100:
            omitted += 1
            continue
        items.append(
            {
                "path": str(path),
                "line": region["start"]["line"] + 1,
                "column": region["start"]["character"] + 1,
                "end_line": region["end"]["line"] + 1,
                "end_column": region["end"]["character"] + 1,
            }
        )
    return {"locations": items, "omitted": omitted, "position_encoding": "utf-16"}
