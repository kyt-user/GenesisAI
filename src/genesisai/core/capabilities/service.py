"""Host-owned lifecycle. Draft tools expose no acceptance mutation to a model."""

from copy import deepcopy
from genesisai.shared.skill_graph import resolve_stage

from genesisai.core.capabilities.contracts import asset_files, relative_path, validate
from genesisai.core.capabilities.execution import mcp_spec, run_tool
from genesisai.core.capabilities.store import CapabilityStore
from genesisai.shared.ids import uid
from genesisai.shared.security import ToolError


class CapabilityService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = CapabilityStore(runtime.store.root, runtime.store.record.project_id)
        self.model = None

    @staticmethod
    def protocol():
        from genesisai.core.capabilities.protocol import protocol
        return protocol()

    @staticmethod
    def summary(item, *, content=False):
        package = item["package"]
        registration, report = item["registration"], item["report"] or {}
        registered = (registration and registration["enabled"] and registration["revision"] == item["revision"]
                      and registration["report_id"] == report.get("id") and report.get("passed"))
        result = {"id": item["id"], "kind": package["kind"], "name": package["name"], "description": package["description"],
                  "source": "external", "scope": package["scope"], "frozen": item["frozen"], "revision": item["revision"], "digest": item["digest"],
                  "report": item["report"], "registration": item["registration"],
                  "state": "registered" if registered else "tested" if report.get("passed") else "draft"}
        if content:
            result["package"] = package
        return result

    def create(self, kind, args):
        self._check_mutation()
        package = {"kind": kind, "scope": "project", "definition": {}, "files": {}, "tests": [], **deepcopy(args)}
        package["files"] = self._import_files(package["files"])
        package["tests"] = self._import_tests(package["tests"])
        if package["kind"] != kind:
            raise ToolError("capability_invalid", "创建入口与能力类型不一致")
        self._check_name(package)
        item = self.store.create(package)
        self.runtime.store.record.run_runtime["capability_target"] = {"id": item["id"]}
        self.runtime.store.commit()
        return self.summary(item, content=True)

    def _check_name(self, package):
        validate(package)
        name = package["name"]
        if package["kind"] == "tool" and name in self.runtime.registry:
            raise ToolError("capability_conflict", "工具名称已被使用，请使用独立名称")
        if package["kind"] == "skill" and name in self.runtime.skills.items:
            raise ToolError("capability_conflict", "Skill 名称已被使用，外挂不能覆盖内置能力")

    def revise(self, args):
        self._check_mutation()
        item = self.store.get(args["id"])
        if item["revision"] != args["expected_revision"]:
            raise ToolError("revision_conflict", "草稿已修改，请先读取当前修订")
        package, changes = deepcopy(item["package"]), deepcopy(args["changes"])
        if not changes or changes.keys() - {"description", "definition", "files", "directory", "tests", "remove_tests"}:
            raise ToolError("capability_invalid", "changes 仅接受 description/definition/files/directory/tests/remove_tests")
        if "description" in changes:
            package["description"] = changes["description"]
        if "definition" in changes:
            for key, value in changes["definition"].items():
                if value is None:
                    package["definition"].pop(key, None)
                else:
                    package["definition"][key] = value
        files = changes.get("files", {})
        for name, reference in files.items():
            relative_path(name)
            if reference is None:
                package["files"].pop(name, None)  # Remove the reference only; earlier assets persist.
        package["files"].update(self._import_files({k: v for k, v in files.items() if v is not None}))
        if "directory" in changes:
            from genesisai.core.capabilities.files import files_under
            root = self._source_path(changes["directory"])
            package["files"].update(self._import_files({p.relative_to(root).as_posix(): {"path": str(p)} for p in files_under(root)}))
        cases = {case["name"]: case for case in package["tests"]}
        for name in changes.get("remove_tests", []):
            cases.pop(name, None)
        for case in self._import_tests(changes.get("tests", [])):
            cases[case["name"]] = case
        package["tests"] = list(cases.values())
        return self.summary(self.store.revise(item["id"], item["revision"], package), content=True)

    def freeze(self, args):
        self._check_mutation()
        return self.summary(self.store.freeze(args["id"], args["expected_revision"]), content=True)

    def _source_path(self, value):
        from pathlib import Path
        from genesisai.core.capabilities.files import reject_link
        raw = Path(value)
        if not raw.is_absolute():
            access = self.runtime.access
            root = access.roots[0] if access.roots else access.output
            root = root if root.is_dir() else root.parent
            raw = next((p for p in (root / raw, access.output / raw) if p.exists()), root / raw)
        for path in (raw, *raw.parents):
            reject_link(path)
        return self.runtime.access.read(value)

    def _import_files(self, files):
        if not isinstance(files, dict):
            raise ToolError("capability_invalid", "文件须为相对路径到 path 或 sha256/size 引用的映射")
        result = {}
        for name, reference in files.items():
            relative_path(name)
            if isinstance(reference, dict) and set(reference) == {"path"}:
                path = self._source_path(reference["path"])
                with self.store.transaction():
                    result[name] = self.store.assets.import_file(path)
            else:
                asset_files({name: reference})
                self.store.assets.resolve(reference)
                result[name] = reference
        asset_files(result)
        return result

    def _import_tests(self, tests):
        if not isinstance(tests, list):
            raise ToolError("capability_invalid", "tests 必须为数组")
        result = []
        for case in deepcopy(tests):
            if not isinstance(case, dict) or not isinstance(case.get("name"), str) or not case["name"].strip():
                raise ToolError("capability_invalid", "每个用例需要唯一非空 name")
            if "fixtures" in case:
                case["fixtures"] = self._import_files(case["fixtures"])
            result.append(case)
        if len({case["name"] for case in result}) != len(result):
            raise ToolError("capability_invalid", "测试用例名称不能重复")
        return result

    def _check_mutation(self):
        if self.runtime.store.record.run_runtime.get("constraints", {}).get("read_only"):
            raise ToolError("task_constraint", "当前任务仅允许讨论或读取，不能修改能力草稿")

    def inspect(self, identity=None):
        return self.summary(self.store.get(identity), content=True) if identity else {"drafts": [self.summary(i) for i in self.store.list()]}

    def preview_test(self, args):
        item = self.store.get(args["id"])
        self._check_revision(item, args)
        if not item["frozen"]:
            raise ToolError("capability_unfrozen", "草稿尚未冻结，请先使用 freeze_capability")
        package = item["package"]
        return {"target": package["name"], "capability_id": item["id"], "revision": item["revision"], "digest": item["digest"],
                "kind": package["kind"], "tests": [case["name"] for case in package["tests"]],
                "definition": package["definition"], "workspace": "独立的测试目录，不使用真实项目目录"}

    @staticmethod
    def _check_revision(item, args):
        if item["revision"] != args["revision"] or item["digest"] != args["digest"]:
            raise ToolError("revision_conflict", "草稿修订或摘要已变化，请重新读取")

    def test(self, args):
        return self._submit_trial('capability_test', args)

    def try_draft(self, args):
        return self._submit_trial('capability_try', args)

    def _submit_trial(self, operation, args):
        from genesisai.core.jobs.operations import runtime_snapshot
        item = self.store.get(args['id'])
        self._check_revision(item, args)
        if not item['frozen']:
            raise ToolError('capability_unfrozen', '草稿尚未冻结，请先使用 freeze_capability')
        self._check_constraints(item['package'])
        arguments = deepcopy(args)
        if operation == 'capability_try':
            arguments['fixtures'] = self._import_files(args.get('fixtures', {}))
        snapshot = runtime_snapshot(self.runtime, item['package'])
        job = self.runtime.jobs.start_operation(operation, {'runtime': snapshot, 'args': arguments},
            timeout=args.get('timeout_seconds'), task_id=(self.runtime.tasks.active_task() or {}).get('id'))
        state = self.runtime.jobs.wait(job['job_id'], 5, cancel_on_interrupt=True)
        if state['status'] not in {'queued', 'running', 'stopping'}:
            result = self.runtime.jobs.operation_result(job['job_id'])
            if operation == 'capability_test':
                result['verification_current'] = self.verification_current(item)
            return result
        return {**state, 'capability_id': item['id'], 'revision': item['revision'], 'digest': item['digest'],
                'continuation': '使用 read_job(job_id) 等待并读取实际结果；测试通过前不能注册'}

    def _test_now(self, args, *, progress_path=None):
        from genesisai.core.capabilities.test_suite import run_suite
        return run_suite(self, args, progress_path=progress_path)

    def verification_current(self, item):
        from genesisai.shared.ids import sha
        from pathlib import Path
        changed = self.runtime.store.record.run_runtime.get("changed_files", {})
        tested_hashes = {ref["sha256"] for ref in item['package']["files"].values()}
        current = bool(changed) and all(value in tested_hashes and Path(path).is_file() and sha(Path(path)) == value for path, value in changed.items())
        return current

    def _try_now(self, args):
        item = self.store.get(args["id"])
        self._check_revision(item, args)
        self._check_constraints(item["package"])
        fixtures = self._import_files(args.get("fixtures", {}))
        workspace = self.store.root / "trials" / uid("trial") / "workspace"
        workspace.mkdir(parents=True)
        self.store.assets.materialize(fixtures, workspace)
        bundle = self.store.materialize(item)
        result = self._trial(item, bundle, args["input"], workspace)
        self.store.materialize(item)
        return {"id": item["id"], "revision": item["revision"], "digest": item["digest"], "workspace": str(workspace), "result": result, "registration_eligible": bool(item["report"] and item["report"]["passed"])}

    def _check_constraints(self, package, stage=None):
        constraints = self.runtime.store.record.run_runtime.get("constraints", {})
        network = package["kind"] == "mcp" or package["definition"].get("network", False)
        if package["kind"] == "skill" and stage is not None:
            try:
                resolved = resolve_stage(package['definition']['stages'], stage, self.runtime.catalog.available)
            except ValueError as exc:
                raise ToolError('capability_dependency', str(exc)) from exc
            network |= any(self.runtime.registry.get(name).spec.network for name in resolved['tools'])
        if constraints.get("read_only") or (constraints.get("network_forbidden") and network):
            raise ToolError("task_constraint", "当前任务约束不允许启动此草稿测试")

    def _trial(self, item, bundle, arguments, workspace):
        package = item['package']
        self._check_constraints(package, arguments.get('stage') if package['kind'] == 'skill' else None)
        if package["kind"] == "tool":
            from genesisai.core.capabilities.tool_results import publish
            outcome = run_tool(package, bundle, arguments, workspace, workspace.parent / "logs",
                               owner={key: item[key] for key in ('id', 'revision', 'digest')}, result_max_bytes=self.result_limit(),
                               redaction_secrets=tuple(self.runtime.logger.journal._secrets))
            return publish(self, outcome)
        if package["kind"] == "skill":
            from genesisai.core.capabilities.skill_trial import run_skill_trial
            if self.model is None:
                raise ToolError("model_required", "Skill 测试需要可用模型执行实际任务，不能仅用静态检查代替")
            return run_skill_trial(self.runtime, self.model, item, bundle, arguments, workspace)
        from genesisai.core.extensions.mcp.bridge import McpBridge
        from genesisai.core.tools.loader import ToolLoader
        from genesisai.core.tools.registry import ToolRegistry
        bridge = McpBridge(bundle, ToolRegistry(), ToolLoader())
        spec = mcp_spec(package, bundle, workspace)
        bridge.configure({}, servers=[spec])
        # Test media uses its own state/assets, never the user's live session.
        from genesisai.core.media.images import ImageStore
        from genesisai.core.persistence.session import Session
        from genesisai.shared.security import Access
        session = Session(workspace.parent / "session", workspace=workspace)
        images = ImageStore(session, Access([workspace], workspace, session.root, workspace_root=workspace))
        images.config = self.runtime.images.config
        bridge.image_handler = images.from_mcp
        bridge.image_config = lambda: images.config
        try:
            bridge.connect(spec.name)
            method, params = arguments.get("method"), arguments.get("params", {})
            if method == "tools/call":
                name = params.get("name")
                tool = bridge._tool_name(spec.name, name) if isinstance(name, str) else ""
                if tool not in bridge.registry:
                    raise ToolError("unknown_tool", "测试引用的 MCP 工具不存在")
                bridge.registry.get(tool).validate_arguments(params.get("arguments", {}))
                return bridge.invoke(spec.name, name, params.get("arguments", {}))
            if method in {"resources/read", "resources/list", "resources/templates/list"}:
                return bridge.resource(spec.name, method, params)
            raise ToolError("capability_invalid", "MCP 测试须进行 tools/call 或资源实际调用")
        finally:
            bridge.close()

    def result_limit(self):
        return self.store.assets.limits['import_max_bytes']

    def collect_tool_result(self, job_id):
        from genesisai.core.capabilities.tool_results import publish
        outcome = self.runtime.jobs.operation_result(job_id, acknowledge=False)
        result = publish(self, outcome, job_id=job_id)
        self.runtime.jobs.operation_result(job_id)
        return result

    def collect_tool_logs(self, job_id):
        from genesisai.core.capabilities.tool_results import logs
        return logs(self, job_id)

    def read_tool_result(self, ref, *, offset=0, limit=200000, page_tokens=None):
        from genesisai.core.capabilities.tool_results import read
        return read(self, ref, offset=offset, limit=limit, page_tokens=page_tokens)

    def read_artifact(self, args):
        from genesisai.core.capabilities.artifacts import read
        return read(self.store, args, session=self.runtime.store, access=self.runtime.access)

    def preview_registration(self, args):
        item = self.store.get(args["id"])
        self._check_revision(item, args)
        if not item["frozen"]:
            raise ToolError("capability_unfrozen", "草稿尚未冻结，请先使用 freeze_capability")
        package = item["package"]
        for other in self.store.active():
            if other["id"] != item["id"] and (other["package"]["kind"], other["package"]["name"]) == (package["kind"], package["name"]):
                raise ToolError("capability_conflict", "另一作用域存在同名已注册能力，请先明确停用或使用独立名称")
        if package["kind"] == "tool" and package["name"] in self.runtime.registry:
            if self.runtime.registry.get(package["name"]).provider != item["id"]:
                raise ToolError("capability_conflict", "工具名称已被其他能力占用")
        if package["kind"] == "skill":
            existing = self.runtime.skills.items.get(package["name"])
            if existing and existing.capability_id != item["id"]:
                raise ToolError("capability_conflict", "Skill 名称已被占用")
        report = item["report"] or {}
        if not report.get("passed") or report.get("digest") != item["digest"]:
            raise ToolError("capability_untested", "此版本没有通过实际测试，不能申请注册")
        self.store.materialize(item)
        return {"target": item["package"]["name"], "kind": item["package"]["kind"], "scope": item["package"]["scope"],
                "capability_id": item["id"], "revision": item["revision"], "digest": item["digest"], "report_id": report["id"],
                "tests": [{"name": case["name"], "passed": case["passed"], "result": case.get("result")} for case in report["cases"]],
                "description": item["package"]["description"], "definition": item["package"]["definition"],
                "acceptance": "请查看测试结果并试用；仅在满意后确认注册此版本"}

    def register(self, args):
        from genesisai.core.tools.permissions import accepted_tool
        from genesisai.shared.security import digest
        authorization = accepted_tool.get()
        preview = self.preview_registration(args)
        if (not authorization or authorization["name"] != "register_capability"
                or authorization["digest"] != digest({**preview, "args": args})):
            raise ToolError("acceptance_required", "正式注册必须由用户验收当前测试版本，模型不能自行授权")
        item = self.store.publish(args["id"], args["revision"], args["digest"], preview["report_id"], self.runtime.store.id)
        self.runtime.refresh_capabilities()
        return self.summary(item)
