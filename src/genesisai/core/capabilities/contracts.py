"""Pure-data capability packages; validation never imports generated code."""

import hashlib
import json
import re
from pathlib import PurePosixPath

from jsonschema.validators import validator_for
from referencing import Registry

from genesisai.core.tools.external_schema import validator
from genesisai.shared.skill_graph import validate_stages
from genesisai.shared.security import ToolError


KINDS = {"tool", "skill", "mcp"}
NAME = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


def schema_validator(schema):
    implementation = validator_for(schema)
    implementation.check_schema(schema)
    return implementation(schema, registry=Registry())


def fail(message):
    raise ToolError("capability_invalid", message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(package):
    return hashlib.sha256(encode(package).encode("utf-8")).hexdigest()


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        fail("包内路径必须为 1–240 字符")
    path = PurePosixPath(value)
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if (path.is_absolute() or "\\" in value or ":" in value or "\0" in value
            or any(part in {"", ".", "..", ".git", ".genesisai"} or part.endswith((".", " "))
                   or part.split(".")[0].casefold() in reserved for part in value.split("/"))):
        fail("包内路径须为普通相对路径，不能越界、使用链接或保留名称")
    return path.as_posix()


def asset_files(files):
    if not isinstance(files, dict):
        fail("files 须为相对路径与资产引用的映射")
    names = set()
    for name, reference in files.items():
        relative_path(name)
        folded = name.casefold()
        if folded in names or any(folded.startswith(old + "/") or old.startswith(folded + "/") for old in names):
            fail("包内文件路径冲突")
        names.add(folded)
        if (not isinstance(reference, dict) or set(reference) != {"sha256", "size"}
                or not isinstance(reference["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])
                or type(reference["size"]) is not int or reference["size"] < 0):
            fail("资产引用须为 sha256/size，源文件请通过 path 引用导入")


def validate(package, *, frozen=False):
    if not isinstance(package, dict) or set(package) != {"kind", "name", "description", "scope", "definition", "files", "tests"}:
        fail("能力包必须且只能包含 kind/name/description/scope/definition/files/tests")
    kind = package["kind"]
    if kind not in KINDS or not isinstance(package["name"], str) or not NAME.fullmatch(package["name"]):
        fail("能力类型或名称无效；名称使用小写字母、数字、下划线，长度 2–48")
    if package["scope"] not in {"user", "project"}:
        fail("scope 必须为 user 或 project")
    if not isinstance(package["description"], str) or not 1 <= len(package["description"].strip()) <= 500:
        fail("description 须为 1–500 字符")
    asset_files(package["files"])
    definition = package["definition"]
    if not isinstance(definition, dict):
        fail("definition 必须为对象")
    if not isinstance(package["tests"], list):
        fail("tests 须为测试用例列表")
    if kind == "tool" and (package["name"].startswith("mcp_") or package["name"] in {"connect_mcp", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}):
        fail("此名称属于内置 MCP 桥接命名空间，请使用独立工具名称")
    allowed = {
        "tool": {"runner", "entry", "command", "parameters", "output_schema", "timeout_seconds", "routes", "network", "env_from"},
        "skill": {"entry", "stages"},
        "mcp": {"transport", "command", "url", "read_only", "env_from", "headers_from", "request_timeouts"},
    }[kind]
    if unknown := definition.keys() - allowed:
        fail(f"{kind} definition 不接受字段 {sorted(unknown)}；允许字段为 {sorted(allowed)}，增量修订用 null 移除误加字段")
    if kind == "tool" and "runner" in definition and definition["runner"] not in ("python", "command"):
        fail('definition.runner 必须是字符串 "python" 或 "command"；Python 源文件路径放在 definition.entry')
    for case in package["tests"]:
        if not isinstance(case, dict) or case.keys() - {"name", "input", "expect", "expect_error", "fixtures"}:
            fail("每个 tests 用例仅接受 name/input/expect/expect_error/fixtures；不接受测试脚本 path")
    if not frozen:
        return package
    if kind == "tool":
        allowed = {"runner", "entry", "command", "parameters", "output_schema", "timeout_seconds", "routes", "network", "env_from"}
        if not {"runner", "parameters", "output_schema"} <= definition.keys() or definition.keys() - allowed:
            fail("Tool definition 缺少必需字段：" + ", ".join(sorted({"runner", "parameters", "output_schema"} - definition.keys())))
        if definition["runner"] == "python":
            if "command" in definition or "entry" not in definition:
                fail("python 运行器只接受 entry，不接受 command")
            _entry(definition, package["files"], ".py")
        elif definition["runner"] == "command":
            if "entry" in definition:
                fail("command 运行器不接受 entry")
            _command(definition.get("command"))
        else:
            fail("runner 必须为 python 或 command")
        validator(definition["parameters"])
        if definition["parameters"].get("type") != "object":
            fail("工具输入 Schema 须为 object")
        schema_validator(definition["output_schema"])
        if not isinstance(definition.get("routes", []), list) or any(not isinstance(r, str) or not r for r in definition.get("routes", [])):
            fail("routes 须为非空字符串列表")
        if type(definition.get("network", False)) is not bool:
            fail("network 须为布尔值")
        _environment(definition.get('env_from', {}))
    elif kind == "skill":
        if set(definition) != {"entry", "stages"}:
            fail("Skill definition 必须且只能包含 entry/stages")
        _entry(definition, package["files"], ".md")
        try:
            validate_stages(definition['stages'])
        except ValueError as exc:
            fail(str(exc))
    else:
        transport = definition.get("transport")
        fields = {"transport", "read_only", "request_timeouts"}
        if transport == "stdio":
            fields |= {"command", "env_from"}
            _command(definition.get("command"))
            _environment(definition.get("env_from", {}))
        elif transport == "streamable_http":
            from urllib.parse import urlsplit
            fields |= {"url", "headers_from"}
            endpoint = urlsplit(definition.get("url", ""))
            if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.fragment:
                fail("远程 MCP 需要无内嵌凭据的 HTTP(S) URL")
            _environment(definition.get("headers_from", {}), headers=True)
        else:
            fail("MCP transport 必须为 stdio 或 streamable_http")
        if type(definition.get("read_only")) is not bool or definition.keys() - fields:
            fail("MCP 定义字段无效；需要 read_only，不混用两种传输配置")
    if kind == 'mcp':
        from genesisai.core.extensions.mcp.timeouts import McpTimeouts
        try:
            McpTimeouts.parse(definition.get('request_timeouts'))
        except ValueError as exc:
            fail(str(exc))
    if kind == 'tool' and 'timeout_seconds' in definition:
        timeout = definition['timeout_seconds']
        if type(timeout) is not int or timeout < 1:
            fail("能力单次请求 timeout_seconds 必须为正整数秒")
    cases = package["tests"]
    if not isinstance(cases, list) or not cases:
        fail("tests 须包含实际执行用例，无数量上限")
    if len({case.get("name") for case in cases if isinstance(case, dict)}) != len(cases):
        fail("测试用例名称须唯一")
    for case in cases:
        if not isinstance(case, dict) or not {"name", "input"} <= case.keys() or case.keys() - {"name", "input", "expect", "expect_error", "fixtures"}:
            fail("测试用例需要 name/input 和 expect 或 expect_error，可选 fixtures")
        if not isinstance(case["name"], str) or not case["name"].strip() or not isinstance(case["input"], dict):
            fail("测试用例 name 须非空，input 须为对象")
        if ("expect" in case) == ("expect_error" in case):
            fail("每个测试须指定 expect JSON Schema 或 expect_error 错误码，二选一")
        if "expect" in case:
            if not isinstance(case["expect"], dict) or not case["expect"]:
                fail("expect 必须为非空 JSON Schema")
            schema_validator(case["expect"])
        elif not isinstance(case["expect_error"], str) or not case["expect_error"]:
            fail("expect_error 须为非空错误码")
        asset_files(case.get("fixtures", {}))
        if kind == 'skill' and (not isinstance(case['input'].get('stage'), str) or case['input']['stage'] not in definition['stages']):
            fail('Skill 测试 input.stage 须为定义中的阶段')
        if kind == "skill" and (not isinstance(case["input"].get("prompt"), str) or not case["input"]["prompt"].strip()):
            fail("Skill 测试 input.prompt 须为实际任务")
    if not any("expect" in case for case in cases):
        fail("至少需要一个预期成功的实际执行用例")
    return package


def _entry(definition, files, suffix):
    entry = relative_path(definition["entry"])
    if entry not in files or not entry.lower().endswith(suffix) or not files[entry]["size"]:
        fail(f"entry 必须指向包内非空的 {suffix} 文件")


def _command(command):
    if not isinstance(command, list) or not command or any(not isinstance(p, str) or not p or "\0" in p for p in command):
        fail("command 须为非空参数数组，支持 {python} 和 {bundle} 占位符")


def _environment(mapping, *, headers=False):
    if not isinstance(mapping, dict):
        fail("环境变量引用须为映射")
    for key, value in mapping.items():
        pattern = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+" if headers else r"[A-Za-z_][A-Za-z0-9_]*"
        if not isinstance(key, str) or not re.fullmatch(pattern, key) or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            fail("只能保存环境变量名称引用，不能保存密钥值")
        if headers and key.casefold() in {"accept", "content-type", "content-length", "host", "mcp-session-id", "mcp-protocol-version", "last-event-id"}:
            fail("headers_from 不能覆盖协议保留字段")
