"""Tool Runtime 共用的声明、契约与 JSON Schema 参数校验。"""

from __future__ import annotations

import sys
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
        self._validate_schema()
        schema = self.parameters or {}
        if not isinstance(args, dict):
            raise SchemaValidationError("工具参数必须是 JSON object")

        required = schema.get("required", [])
        for name in required:
            if name not in args:
                raise SchemaValidationError(f"缺少必需字段: '{name}'")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(args) - set(properties)
            if unknown:
                raise SchemaValidationError(f"包含未知字段: {sorted(unknown)[0]}")

        validators = {
            "string": lambda value: isinstance(value, str),
            "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
            "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": lambda value: isinstance(value, bool),
            "array": lambda value: isinstance(value, list),
            "object": lambda value: isinstance(value, dict),
        }
        for name, definition in properties.items():
            if not isinstance(definition, dict):
                raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的定义必须是对象")
            if name not in args or definition.get("type") is None:
                continue
            expected = definition["type"]
            validator = validators.get(expected)
            if validator is None:
                raise SchemaConfigError(f"Schema 配置无效：不支持的类型 '{expected}'")
            if not validator(args[name]):
                raise SchemaValidationError(f"字段 '{name}' 必须是 {expected} 类型")
            if "enum" in definition and args[name] not in definition["enum"]:
                raise SchemaValidationError(f"字段 '{name}' 不在允许的选项中")
            self._validate_bounds(args[name], definition, name)

    def _validate_schema(self) -> None:
        schema = self.parameters or {}
        if not isinstance(schema, dict):
            raise SchemaConfigError("Schema 配置无效：parameters 必须是对象")
        if schema.get("type") not in (None, "object"):
            raise SchemaConfigError(f"不支持的根类型: {schema.get('type')}")
        if schema.get("additionalProperties") is not False:
            raise SchemaConfigError("Schema 配置无效：必须禁止未知参数")
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise SchemaConfigError("Schema 配置无效：required 必须是字符串数组")
        if not isinstance(properties, dict):
            raise SchemaConfigError("Schema 配置无效：properties 必须是对象")
        if any(name not in properties for name in required):
            raise SchemaConfigError("Schema 配置无效：required 字段没有属性定义")
        allowed = {"string", "integer", "number", "boolean", "array", "object"}
        for name, definition in properties.items():
            if not isinstance(name, str) or not isinstance(definition, dict):
                raise SchemaConfigError("Schema 配置无效：属性定义必须是对象")
            if definition.get("type") not in allowed:
                raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 类型无效")
            self._validate_property_schema(name, definition, allowed)

    @staticmethod
    def _validate_property_schema(name: str, definition: dict, allowed: set[str]) -> None:
        for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
            for field in (lower, upper):
                value = definition.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的 {field} 无效")
            if lower in definition and upper in definition and definition[lower] > definition[upper]:
                raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的边界颠倒")
        for field in ("minimum", "maximum"):
            value = definition.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的 {field} 无效")
        if "minimum" in definition and "maximum" in definition and definition["minimum"] > definition["maximum"]:
            raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的数值边界颠倒")
        if definition["type"] == "array":
            items = definition.get("items")
            if not isinstance(items, dict) or items.get("type") not in allowed:
                raise SchemaConfigError(f"Schema 配置无效：属性 '{name}' 的 items 无效")

    @classmethod
    def _validate_bounds(cls, value, definition, name):
        if isinstance(value, str):
            if len(value) < definition.get("minLength", 0) or len(value) > definition.get("maxLength", float("inf")):
                raise SchemaValidationError(f"字段 '{name}' 长度超限")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < definition.get("minimum", -float("inf")) or value > definition.get("maximum", float("inf")):
                raise SchemaValidationError(f"字段 '{name}' 数值超限")
        elif isinstance(value, list):
            if len(value) < definition.get("minItems", 0) or len(value) > definition.get("maxItems", float("inf")):
                raise SchemaValidationError(f"字段 '{name}' 项数超限")
            item_schema = definition.get("items", {})
            for item in value:
                if item_schema.get("type") == "string" and not isinstance(item, str):
                    raise SchemaValidationError(f"字段 '{name}' 数组元素必须是字符串")
                if item_schema.get("type") == "object":
                    if not isinstance(item, dict):
                        raise SchemaValidationError(f"字段 '{name}' 数组元素必须是对象")
                    if "properties" in item_schema:
                        Tool(name=name, description="", parameters=item_schema)._validate_args(item)
                cls._validate_bounds(item, item_schema, name)


@dataclass(frozen=True)
class ToolSpec:
    """由内置代码固定的参数、安全与运行契约。"""

    parameters: dict
    permission: str
    path_scope: str = "none"
    network: bool = False
    side_effect: bool = False
    timeout_seconds: int = 30
    max_output_chars: int = 12000
    platforms: tuple[str, ...] = ("win32", "linux", "darwin")
    dependencies: tuple[str, ...] = ()
    history_lookup: bool = False
    routes: tuple[str, ...] = ()
    launches_service: bool = False
    awaits_input: bool = False
    external_schema: bool = False
    completion_field: str | None = None

    def validate(self) -> None:
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
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds < 1:
            raise SchemaConfigError("Spec timeout_seconds 无效")
        if type(self.max_output_chars) is not int or self.max_output_chars < 256:
            raise SchemaConfigError("Spec max_output_chars 至少为 256，以容纳完整结果的引用")
        if not isinstance(self.platforms, tuple) or not self.platforms or any(not isinstance(item, str) for item in self.platforms):
            raise SchemaConfigError("Spec platforms 无效")
        if not isinstance(self.dependencies, tuple) or any(not isinstance(item, str) or not item for item in self.dependencies):
            raise SchemaConfigError("Spec dependencies 无效")
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
        return "缺少可选依赖，请安装 genesisai[office]：" + ", ".join(missing) if missing else None


@dataclass(frozen=True)
class ToolDescriptor:
    """YAML 目录信息与代码 Spec 组合后的只读注册项。"""

    name: str
    enabled: bool
    category: str
    description: str
    directory: Path
    spec: ToolSpec

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
    def __init__(self, code, message, **data):
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


def object_schema(required: list[str], **properties) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
