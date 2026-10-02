from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["names"], names={"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 64}, "minItems": 1, "maxItems": 2}), permission="core")

