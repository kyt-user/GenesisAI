"""Run a Skill against its declared dependencies in a separate agent session."""

from copy import copy, deepcopy
from dataclasses import replace
import time

from genesisai.agent.loop import Runner
from genesisai.core.capabilities.contracts import fingerprint
from genesisai.core.capabilities.artifacts import collect
from genesisai.core.tools.base import HostBoundTool
from genesisai.core.persistence.session import Session
from genesisai.shared.security import Access, ToolError
from genesisai.shared.time_limits import tool_deadline
from genesisai.shared.skill_graph import resolve_stage
from genesisai.core.capabilities.trial_adapters import adapters


def run_skill_trial(parent, model, item, bundle, arguments, workspace):
    from genesisai.core.tools.tool_runtime import ToolRuntime
    package = item['package']
    session = Session(workspace.parent / "session", workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.record.run_runtime['constraints'] = deepcopy(parent.store.record.run_runtime['constraints'])
    session.commit()
    trial = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), providers=[], permission_mode="auto")
    trial.access.host_access = False
    try:
        resolution = resolve_stage(package['definition']['stages'], arguments.get('stage'), parent.catalog.available)
    except ValueError as exc:
        trial.close()
        raise ToolError('capability_dependency', str(exc)) from exc
    allowed = set(resolution['tools'])
    forbidden = {name for name in parent.registry._items if "capability_builder" in parent.registry.get(name).spec.routes}
    try:
        if allowed & forbidden:
            raise ToolError("capability_dependency", "Skill 试运行不能递归构建、测试或注册能力")
        mcp_owners = {parent.registry.get(name).provider for name in allowed if parent.registry.get(name).category == "mcp"}
        if mcp_owners:
            servers = [replace(parent.mcp.config[name], cwd=str(workspace)) if parent.mcp.config[name].transport == "stdio" else parent.mcp.config[name]
                       for name in mcp_owners if name in parent.mcp.config]
            # New transports and a separate cache: never share a production
            # connection with the trial agent.
            trial.mcp.configure(deepcopy(parent.store.record.mcp_tools), servers=servers)
            for spec in servers:
                trial.mcp.connect(spec.name)
        for name in allowed:
            if name not in trial.registry:
                descriptor = parent.registry.get(name)
                implementation = parent.loader.load(descriptor)
                if isinstance(implementation, HostBoundTool):
                    implementation = adapters(parent).create(descriptor, trial)
                trial.register_tool(descriptor, implementation)
        trial.tool_allowlist = allowed | {"attempt_completion", "read_job", "stop_job"}
        trial.skills.replace_external([({"id": "trial", "revision": 1, "digest": fingerprint(package), "package": package}, bundle)])
        session.record.tool_runtime["active_skills"] = {package["name"]: arguments["stage"]}
        budget = arguments.get("budget", {})
        if not isinstance(budget, dict) or budget.keys() - {"rounds", "tools", "total_tokens", "seconds", "request_timeouts"}:
            raise ToolError("capability_invalid", "Skill input.budget 仅接受 rounds/tools/total_tokens/seconds/request_timeouts")
        seconds = budget.get("seconds")
        if tool_deadline.get():
            remaining = max(.1, tool_deadline.get() - time.monotonic())
            seconds = min(seconds, remaining) if seconds is not None else remaining
        # A shallow model adapter shares only the transport; image resolution and
        # context setup for the trial must not mutate the parent model instance.
        runner = Runner(copy(model), trial, max_rounds=budget.get("rounds"), max_tools=budget.get("tools"),
                        token_budget=budget.get("total_tokens"), seconds=seconds,
                        request_timeouts=budget.get("request_timeouts"))
        result = runner.start(arguments["prompt"])
        if result["status"] != "completed":
            raise ToolError("skill_trial_failed", "Skill 实际任务未完成：" + result["status"])
        artifacts = collect(parent.capabilities.store, item, workspace)
        return {"status": result["status"], "answer": result["answer"], "artifacts": artifacts,
                'session_id': session.id, 'state_root': str(session.root), **resolution}
    finally:
        trial.close()
