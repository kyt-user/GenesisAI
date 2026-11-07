"""Tool Runtime 共用的声明、契约与 JSON Schema 参数校验。"""

from __future__ import annotations

import sys
import json
from functools import lru_cache
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SchemaValidationError(Exception):
    """工具参数不满足 Schema。"""


class SchemaConfigError(Exception):
    """工具 Schema 本身无效。"""


class Tool:
    """模型可见的工具元数据和参数约束。"""

    def __init__(self, *, name: str, description: str, parameters: dict):
        self.name = name
        self.description = description
        self.parameters = parameters

    def to_openai_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def _validate_args(self, args: object) -> None:
        validator = self._validate_schema()
        if not isinstance(args, dict):
            raise SchemaValidationError('工具参数必须是 JSON object')
        try:
            json.dumps(args, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise SchemaValidationError('工具参数必须是有限数值组成的有效 JSON 数据') from exc
        from referencing.exceptions import Unresolvable
        try:
            error = next(validator.iter_errors(args), None)
        except (Unresolvable, RecursionError) as exc:
            raise SchemaConfigError('工具 Schema 含不可解析或循环引用') from exc
        if error is not None:
            # Do not echo arbitrary input values (including interactive stdin)
            # into diagnostics. The exact path and violated rule are sufficient.
            detail = ''
            if error.validator in {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'} and type(error.instance) in (int, float):
                lower = error.schema.get('minimum', error.schema.get('exclusiveMinimum', '-∞'))
                upper = error.schema.get('maximum', error.schema.get('exclusiveMaximum', '+∞'))
                detail = f'；实际 {error.instance}，允许范围 {lower}..{upper}'
            raise SchemaValidationError(f"字段 '{error.json_path}' 不满足 {error.validator} 约束{detail}；请按工具 Schema 修正参数")

    def _validate_schema(self):
        schema = self.parameters
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise SchemaConfigError('Schema 配置无效：parameters 必须声明 object 类型')
        if schema.get('additionalProperties') is not False:
            raise SchemaConfigError('Schema 配置无效：必须禁止未知参数')
        properties, required = schema.get('properties', {}), schema.get('required', [])
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(name, str) or name not in properties for name in required)):
            raise SchemaConfigError('Schema 配置无效：required 必须引用已声明属性')
        try:
            encoded = json.dumps(schema, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise SchemaConfigError('Schema 配置必须是有效 JSON 数据') from exc
        return _builtin_validator(encoded)


@lru_cache(maxsize=128)
def _builtin_validator(encoded):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from referencing import Registry
    schema = json.loads(encoded)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaConfigError('Schema 配置无效：' + exc.message) from exc
    return Draft202012Validator(schema, registry=Registry())


@dataclass(frozen=True)
class ToolSpec:
    """由内置代码固定的参数、安全与运行契约。"""

    parameters: dict
    permission: str
    path_scope: str = "none"
    network: bool = False
    network_boundary: str = 'public'
    side_effect: bool = False
    timeout_seconds: int = 30
    execution: str = 'foreground'
    execution_timeout_seconds: float | None = None
    max_output_chars: int = 12000
    platforms: tuple[str, ...] = ("win32", "linux", "darwin")
    dependencies: tuple[str, ...] = ()
    dependency_extra: str | None = None
    history_lookup: bool = False
    routes: tuple[str, ...] = ()
    launches_service: bool = False
    awaits_input: bool = False
    external_schema: bool = False
    completion_field: str | None = None
    requires_acceptance: bool = False

    def validate(self) -> None:
        import math
        if self.execution not in {'foreground', 'job'}:
            raise SchemaConfigError('Spec execution 必须是 foreground 或 job')
        if self.execution_timeout_seconds is not None and (
                type(self.execution_timeout_seconds) not in (int, float)
                or not math.isfinite(self.execution_timeout_seconds) or self.execution_timeout_seconds <= 0
                or self.execution != 'job'):
            raise SchemaConfigError('作业执行时限必须是正数，或不设置总时限')
        if self.execution == 'job' and (self.awaits_input or self.requires_acceptance or self.completion_field):
            raise SchemaConfigError('输入、注册验收和完成控制必须由前台处理')
        if type(self.requires_acceptance) is not bool or (self.requires_acceptance and (self.permission != "write" or not self.side_effect)):
            raise SchemaConfigError("用户验收操作必须是有副作用的 write 工具")
        if not isinstance(self.routes, tuple) or not all(isinstance(r, str) and r for r in self.routes):
            raise SchemaConfigError("Spec routes 必须是任务标签元组")
        if type(self.launches_service) is not bool or type(self.awaits_input) is not bool:
            raise SchemaConfigError("服务启动和用户提问标记必须为布尔值")
        if self.awaits_input and (self.permission != "core" or self.side_effect or self.launches_service):
            raise SchemaConfigError("提问工具必须为无副作用的 core 工具")
        if self.completion_field is not None and (not isinstance(self.completion_field, str) or not self.completion_field):
            raise SchemaConfigError("Spec completion_field 必须是结果中的文字字段名")
        if type(self.history_lookup) is not bool or (
            self.history_lookup and (self.side_effect or self.network or self.launches_service or self.awaits_input or self.permission not in {"read", "core"})
        ):
            raise SchemaConfigError("历史查阅工具必须只读且不访问网络")
        if self.permission not in {"core", "read", "write", "network", "shell"}:
            raise SchemaConfigError("Spec permission 无效")
        if self.path_scope not in {"none", "authorized", "output", "workspace"}:
            raise SchemaConfigError("Spec path_scope 无效")
        if not isinstance(self.network, bool) or not isinstance(self.side_effect, bool):
            raise SchemaConfigError("Spec 布尔字段无效")
        if self.network_boundary not in {'public', 'private'} or (self.network_boundary == 'private' and not self.network):
            raise SchemaConfigError('Spec network_boundary 必须是 public/private；private 仅适用于网络工具')
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds < 1:
            raise SchemaConfigError("Spec timeout_seconds 无效")
        if type(self.max_output_chars) is not int or self.max_output_chars < 256:
            raise SchemaConfigError("Spec max_output_chars 至少为 256，以容纳完整结果的引用")
        if not isinstance(self.platforms, tuple) or not self.platforms or any(not isinstance(item, str) for item in self.platforms):
            raise SchemaConfigError("Spec platforms 无效")
        if not isinstance(self.dependencies, tuple) or any(not isinstance(item, str) or not item for item in self.dependencies):
            raise SchemaConfigError("Spec dependencies 无效")
        if self.dependency_extra is not None and (not isinstance(self.dependency_extra, str) or not self.dependency_extra.isidentifier()):
            raise SchemaConfigError('Spec dependency_extra 必须是可选依赖组名')
        if self.permission == "network" and not self.network:
            raise SchemaConfigError("网络工具必须声明 network=True")
        if self.permission != "network" and self.network:
            raise SchemaConfigError("非网络工具不能声明 network=True")
        if self.permission == "write" and not self.side_effect:
            raise SchemaConfigError("写入工具必须声明 side_effect=True")
        if self.permission == "shell" and not self.side_effect:
            raise SchemaConfigError("Shell 工具必须声明 side_effect=True")
        if self.path_scope == "authorized" and self.permission != "read":
            raise SchemaConfigError("authorized 路径只适用于只读工具")
        if self.path_scope == "output" and self.permission != "write":
            raise SchemaConfigError("output 路径只适用于写入工具")
        if self.path_scope == "workspace" and self.permission not in {"read", "write", "shell"}:
            raise SchemaConfigError("workspace 路径不适用于当前权限")
        if self.external_schema:
            from .external_schema import validator
            validator(self.parameters)
            return
        Tool(name="spec_check", description="spec", parameters=self.parameters)._validate_schema()

    def unavailable_reason(self) -> str | None:
        if sys.platform not in self.platforms:
            return f"当前平台 {sys.platform} 不受支持"
        missing = [name for name in self.dependencies if importlib.util.find_spec(name) is None]
        return (('缺少可选依赖，请安装 genesisai[' + self.dependency_extra + ']：')
                if self.dependency_extra else '缺少可选依赖：') + ', '.join(missing) if missing else None


@dataclass(frozen=True)
class ToolDescriptor:
    """YAML 目录信息与代码 Spec 组合后的只读注册项。"""

    name: str
    enabled: bool
    category: str
    description: str
    directory: Path
    spec: ToolSpec
    source: str = "builtin"
    scope: str = "application"
    provider: str | None = None

    def validate_arguments(self, args):
        if self.spec.external_schema:
            from .external_schema import validator
            if not isinstance(args, dict):
                raise SchemaValidationError("工具参数必须是对象")
            error = next(validator(self.spec.parameters).iter_errors(args), None)
            if error:
                raise SchemaValidationError(f"{error.json_path}: {error.message}")
        else:
            Tool(name=self.name, description=self.description, parameters=self.spec.parameters)._validate_args(args)

    def definition(self) -> dict:
        return Tool(name=self.name, description=self.description, parameters=self.spec.parameters).to_openai_format()


class PartialToolResult(dict):
    """A failed operation with known partial results and observations."""
    def __init__(self, code, message, /, **data):
        super().__init__(data)
        self.error = {"code": code, "message": message, "retryable": False}


class HostBoundTool:
    """Adapter for a persistent host-owned service, e.g. a live MCP connection."""

    def __init__(self, service):
        self.host_service = service


@dataclass
class ToolContext:
    """工具实现可使用的受控运行依赖。"""

    store: Any
    access: Any
    network: Any
    providers: list
    runtime: Any = None
    # Explicitly injected live clients keep their identity on the host.
    hosted_network: bool = True
    hosted_providers: bool = True
    call_id: str | None = None
    authorization: dict | None = None


def object_schema(required: list[str], **properties) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
