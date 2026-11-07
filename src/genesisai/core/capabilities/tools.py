"""Builtin lifecycle interfaces; external packages never register these tools."""

from pathlib import Path
from genesisai.shared.skill_graph import STAGES_SCHEMA

from genesisai.core.tools.base import HostBoundTool, ToolDescriptor, ToolSpec, object_schema


TEXT = {"type": "string", "minLength": 1}
ASSET = {"oneOf": [object_schema(["path"], path=TEXT),
                    object_schema(["sha256", "size"], sha256={"type": "string", "pattern": "^[a-f0-9]{64}$"}, size={"type": "integer", "minimum": 0})]}
FILES = {"type": "object", "additionalProperties": ASSET}
TESTS = {"type": "array", "items": object_schema(["name"], name=TEXT, input={"type": "object"},
    expect={"type": ["object", "boolean"]}, expect_error=TEXT, fixtures=FILES)}
DEFINITIONS = {
    "tool": object_schema([], runner={"type": "string", "enum": ["python", "command"]}, entry=TEXT,
        command={"type": "array", "items": TEXT, "minItems": 1}, parameters={"type": "object"},
        output_schema={"type": ["object", "boolean"]}, timeout_seconds={"type": "integer", "minimum": 1},
        routes={"type": "array", "items": TEXT}, network={"type": "boolean"},
        env_from={"type": "object", "additionalProperties": TEXT}),
    "skill": object_schema([], entry=TEXT, stages=STAGES_SCHEMA),
    "mcp": object_schema([], transport={"type": "string", "enum": ["stdio", "streamable_http"]},
        command={"type": "array", "items": TEXT, "minItems": 1}, url=TEXT, read_only={"type": "boolean"},
        env_from={"type": "object", "additionalProperties": TEXT}, headers_from={"type": "object", "additionalProperties": TEXT},
        request_timeouts=object_schema([],
            connect={'type': 'number', 'exclusiveMinimum': 0},
            first_response={'type': 'number', 'exclusiveMinimum': 0},
            idle={'type': 'number', 'exclusiveMinimum': 0},
            total={'type': ['number', 'null'], 'exclusiveMinimum': 0})),
}
DRAFT = object_schema(["name", "description"], name={**TEXT, "maxLength": 48},
                     description={**TEXT, "maxLength": 500}, scope={"type": "string", "enum": ["project", "user"]},
                     definition={"type": "object"}, files=FILES, tests=TESTS)
CHANGES = object_schema([], description={**TEXT, "maxLength": 500}, definition={"type": "object"},
                        files={"type": "object", "additionalProperties": {"anyOf": [ASSET, {"type": "null"}]}},
                        directory=TEXT, tests=TESTS, remove_tests={"type": "array", "items": TEXT})

REVISION = {"id": TEXT, "revision": {"type": "integer", "minimum": 1}, "digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}


class LifecycleTool(HostBoundTool):
    def __init__(self, service, operation, kind=None):
        super().__init__(service)
        self.operation, self.kind = operation, kind

    def observe(self, args, payload):
        from genesisai.core.tools.observations import Observation
        if self.operation == "test" and payload.get("verification_current"):
            return [Observation("verification", passed=payload["report"]["passed"], detail="capability:" + payload["digest"])]
        return []

    def prepare(self, context, args):
        if self.operation in {"test", "try"}:
            return self.host_service.preview_test(args)
        if self.operation == "register":
            return self.host_service.preview_registration(args)
        return None

    def execute(self, context, args):
        if self.operation == "create":
            return self.host_service.create(self.kind, args)
        if self.operation == "inspect":
            if args.get("protocol"):
                return self.host_service.protocol()
            return self.host_service.inspect(args.get("id"))
        if self.operation == "revise":
            return self.host_service.revise(args)
        if self.operation == "freeze":
            return self.host_service.freeze(args)
        if self.operation == "test":
            return self.host_service.test(args)
        if self.operation == "try":
            return self.host_service.try_draft(args)
        if self.operation == "read_artifact":
            return self.host_service.read_artifact(args)
        return self.host_service.register(args)


def register_lifecycle(runtime):
    service = runtime.capabilities
    declarations = []
    for kind in ("tool", "skill", "mcp"):
        schema = {**DRAFT, "properties": {**DRAFT["properties"], "definition": DEFINITIONS[kind]}}
        declarations.append(("create_" + kind, "create", kind, schema, "core", False,
                             f"创建外挂 {kind} 草稿元信息；实现通过文件引用增量加入，冻结测试后由用户验收注册"))
    declarations.extend([
        ("inspect_capabilities", "inspect", None, object_schema([], id=TEXT, protocol={"type": "boolean"}), "core", False, "查看能力草稿、版本、测试报告；protocol=true 获取完整创建协议"),
        ("read_capability_artifact", "read_artifact", None, object_schema(['id'], id=TEXT,
            mode={'type': 'string', 'enum': ['metadata', 'text', 'base64']},
            offset={'type': 'integer', 'minimum': 0}, limit={'type': 'integer', 'minimum': 1, 'maximum': 200000}),
            'core', False, "按 Skill 测试产物引用读取元数据、分页文本或二进制字节；核对哈希和当前作用域，不开放程序状态目录"),
        ("revise_capability", "revise", None, object_schema(["id", "expected_revision", "changes"], id=TEXT, expected_revision={"type": "integer", "minimum": 1}, changes=CHANGES), "core", False, "按修订号增量更新元信息、文件引用和具名测试；files=null 移除引用，directory 导入目录快照；修改后须重新冻结测试验收"),
        ("freeze_capability", "freeze", None, object_schema(["id", "expected_revision"], id=TEXT, expected_revision={"type": "integer", "minimum": 1}), "core", False, "校验完整草稿并冻结当前版本；冻结成功后才能测试/试用"),
        ("test_capability", "test", None, object_schema(list(REVISION), **REVISION,
            resume_report_id={"type": "string", "pattern": "^test_[a-f0-9]{16}$"},
            timeout_seconds={"type": "number", "exclusiveMinimum": 0},
            reconcile=object_schema(["case", "action"], case=TEXT, action={"type": "string", "enum": ["retry", "mark_failed"]})),
            "shell", False, "执行冻结版本的完整测试；resume_report_id 续跑已停止套件并保留已完成用例，结果不明用例须显式 reconcile"),
        ("try_capability", "try", None, object_schema([*REVISION, "input"], **REVISION, input={"type": "object"}, fixtures=FILES, timeout_seconds={"type": "number", "exclusiveMinimum": 0}), "shell", False, "在独立工作目录试用草稿并返回实际结果；试用不能代替全部测试"),
        ("register_capability", "register", None, object_schema(list(REVISION), **REVISION), "write", True, "展示当前版本测试报告，等待用户明确验收；确认后注册到外挂能力库，自动权限模式也必须等待"),
    ])
    for name, operation, kind, parameters, permission, acceptance, description in declarations:
        spec = ToolSpec(parameters=parameters, external_schema=True, permission=permission,
                        side_effect=permission in {"write", "shell"}, requires_acceptance=acceptance,
                        timeout_seconds=30,
                        max_output_chars=16000, routes=("capability_builder",))
        spec.validate()
        runtime.registry.register(ToolDescriptor(name, True, "kernel", description, Path(__file__).parent, spec))
        runtime.loader.register(name, LifecycleTool(service, operation, kind))
