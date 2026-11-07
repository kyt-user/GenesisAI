"""Explicit terminal actions use the same executor and acceptance as the agent."""

import json
import shlex

from genesisai.shared.ids import uid


CREATE_COMMANDS = {"/create_tool": "tool", "/create_skill": "skill", "/create_mcp": "mcp"}


def read_manifest(runtime, filename, *, changes=False):
    path = runtime.access.read(filename)
    # JSON metadata has its own decoding budget; code and fixtures are streamed
    # separately through asset storage and do not consume this allowance.
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError("清单元信息超过 32 MiB 解码预算；请分批增量导入")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or (not changes and value.get("kind") not in {"tool", "skill", "mcp"}):
        raise ValueError("需要能力清单 JSON 对象")
    for files in [value.get("files", {}), *(case.get("fixtures", {}) for case in value.get("tests", []))]:
        for reference in files.values():
            if isinstance(reference, dict) and set(reference) == {"path"}:
                reference["path"] = str(path.parent / reference["path"])
    if "directory" in value:
        value["directory"] = str(path.parent / value["directory"])
    return value


def creation_prompt(text):
    command, _, request = text.partition(" ")
    if command not in CREATE_COMMANDS or not request.strip():
        raise ValueError("用法：/create_tool 需求、/create_skill 需求 或 /create_mcp 需求")
    return f"创造模式：请调用 create_{CREATE_COMMANDS[command]}，为我构建以下外挂能力，完成实际测试后交由我验收注册。\n需求：{request.strip()}"


def execute_capability_command(runtime, text, *, confirm):
    parts = shlex.split(text, posix=False)
    parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts]
    action = parts[1] if len(parts) > 1 else "list"
    service = runtime.capabilities
    if action == "list" and len(parts) <= 2:
        runtime.refresh_capabilities()
        tools, skills = runtime.inventory(), runtime.skills.search(limit=1000)
        return {source: {"tools": tools[source], "skills": [s for s in skills if s["source"] == source],
                         "mcp": [{"name": s.name, "scope": s.scope, "connected": s.name in runtime.mcp.transports}
                                 for s in runtime.mcp.config.values()] if source == "external" else []}
                for source in ("builtin", "external")} | {"drafts": service.inspect()["drafts"], "diagnostics": runtime.capability_diagnostics}
    if action == "protocol" and len(parts) == 2:
        return service.protocol()
    if action == "show" and len(parts) == 3:
        return service.inspect(parts[2])
    if action == "export" and len(parts) == 4:
        item = service.store.get(parts[2])
        path = runtime.access.write(parts[3])
        path.parent.mkdir(parents=True, exist_ok=True)
        from copy import deepcopy
        import shutil
        package = deepcopy(item["package"])
        folder = path.with_name(path.name + ".assets")
        if folder.exists():
            raise ValueError("导出资产目录已存在，请使用新名称")
        folder.mkdir()
        copied = set()
        for files in [package["files"], *(case.get("fixtures", {}) for case in package["tests"])]:
            for name, reference in list(files.items()):
                digest = reference["sha256"]
                if digest not in copied:
                    destination = runtime.access.write(str(folder / digest)) if runtime.access.host_access else runtime.access.write(str((folder / digest).relative_to(runtime.access.output)))
                    with service.store.assets.resolve(reference).open("rb") as source, destination.open("xb") as target:
                        shutil.copyfileobj(source, target, 1024 * 1024)
                    copied.add(digest)
                files[name] = {"path": folder.name + "/" + digest}
        with path.open("x", encoding="utf-8") as handle:
            json.dump(package, handle, ensure_ascii=False, indent=2)
        return {"path": str(path), "id": item["id"], "revision": item["revision"]}
    if action == "import" and len(parts) == 3:
        package = read_manifest(runtime, parts[2])
        return service.create(package["kind"], {k: v for k, v in package.items() if k != "kind"})
    if action == "revise" and len(parts) == 5:
        return service.revise({"id": parts[2], "expected_revision": int(parts[3]), "changes": read_manifest(runtime, parts[4], changes=True)})
    if action == "freeze" and len(parts) == 3:
        item = service.store.get(parts[2])
        return service.freeze({"id": item["id"], "expected_revision": item["revision"]})
    if action == "disable" and len(parts) == 3:
        item = service.store.disable(parts[2])
        runtime.refresh_capabilities()
        return service.summary(item)
    if (action in {"test", "register"} and len(parts) == 3) or (action == "try" and len(parts) == 4):
        item = service.store.get(parts[2])
        args = {"id": item["id"], "revision": item["revision"], "digest": item["digest"]}
        if action == "try":
            trial = read_manifest(runtime, parts[3], changes=True)
            if not isinstance(trial, dict) or "input" not in trial or trial.keys() - {"input", "fixtures"}:
                raise ValueError("试用文件须为 {input: 对象, fixtures: 可选文件映射}")
            args.update(trial)
        call = {"id": uid("manual"), "name": action + "_capability", "arguments": json.dumps(args)}
        result = runtime.execute(call)
        if result.get("pending"):
            if not confirm(result["preview"]):
                return runtime.reject(call)
            digest = runtime.store.record.calls[call["id"]]["digest"]
            result = runtime.execute(call, approved=digest)
        return result
    raise ValueError("用法：/capabilities list|protocol|show ID|import 文件|export ID 文件|revise ID 修订号 增量清单|freeze ID|test ID|try ID 输入文件|register ID|disable ID")
