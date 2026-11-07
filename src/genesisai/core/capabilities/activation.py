"""Project accepted bundles into the existing execution registry."""

import hashlib
import os

from genesisai.core.capabilities.execution import ExternalTool, mcp_spec
from genesisai.core.tools.base import ToolDescriptor, ToolSpec
from genesisai.shared.security import ToolError
from genesisai.shared.skill_graph import all_tools


def refresh(runtime):
    entries = runtime.capabilities.store.active()
    materialized = []
    diagnostics = []
    for item in entries:
        try:
            materialized.append((item, runtime.capabilities.store.materialize(item)))
        except (OSError, ValueError, ToolError) as exc:
            diagnostics.append(f"{item['package']['name']}: {exc}")
    revisions = [(item["id"], item["revision"], item["digest"]) for item, _ in materialized]
    dependencies = {name for item, _ in materialized if item["package"]["kind"] == "skill"
                    for name in all_tools(item["package"]["definition"]["stages"])}
    environment = [(name, hashlib.sha256(os.environ.get(name, "").encode()).hexdigest())
                   for name in sorted({source for item, _ in materialized if item["package"]["kind"] == "mcp"
                                       for source in {**item["package"]["definition"].get("env_from", {}), **item["package"]["definition"].get("headers_from", {})}.values()})]
    signature = (revisions, [(name, runtime.catalog.available(name)) for name in sorted(dependencies)], environment, diagnostics)
    if signature == runtime._capability_signature:
        return
    integrity_diagnostics = list(diagnostics)
    for name in runtime._capability_tools:
        runtime.registry.unregister_dynamic(name)
        runtime.loader.unregister_dynamic(name)
    runtime._capability_tools = set()
    skills, servers = [], []
    occupied = set()
    for item, bundle in materialized:
        package = item["package"]
        identity = package["kind"], package["name"]
        if identity in occupied:
            diagnostics.append(f"{package['name']}: 个人与项目作用域名称冲突，未启用重复项")
            continue
        occupied.add(identity)
        if package["kind"] == "tool":
            name, definition = package["name"], package["definition"]
            if name in runtime.registry:
                diagnostics.append(f"{name}: 与现有工具重名，未启用")
                continue
            network = definition.get("network", False)
            spec = ToolSpec(parameters=definition["parameters"], external_schema=True,
                            permission="network" if network else "shell", network=network,
                            side_effect=True, path_scope="none" if network else "workspace",
                            timeout_seconds=30,
                            routes=tuple(definition.get("routes", [])))
            spec.validate()
            descriptor = ToolDescriptor(name, True, "external", package["description"], bundle, spec,
                                        source="external", scope=package["scope"], provider=item["id"])
            runtime.registry.register(descriptor)
            runtime.loader.register(name, ExternalTool(package, bundle, item["id"], item["revision"], item["digest"]))
            runtime._capability_tools.add(name)
        elif package["kind"] == "skill":
            skills.append((item, bundle))
        else:
            try:
                servers.append(mcp_spec(package, bundle, runtime.store.workspace))
            except ToolError as exc:
                diagnostics.append(f"{package['name']}: {exc}")
    runtime.mcp.configure(runtime.store.record.mcp_tools, servers=servers)
    available_skills = []
    for item, bundle in skills:
        name = item["package"]["name"]
        if name in runtime.skills.items and runtime.skills.items[name].source == "builtin":
            diagnostics.append(f"{name}: 与内置 Skill 重名")
        else:
            available_skills.append((item, bundle))
    runtime.skills.replace_external(available_skills)
    runtime.capability_diagnostics = diagnostics
    runtime._capability_signature = (revisions, [(name, runtime.catalog.available(name)) for name in sorted(dependencies)], environment, integrity_diagnostics)
