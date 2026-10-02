from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema([], cwd={"type": "string", "minLength": 1, "maxLength": 2000}, staged={"type": "boolean"}, paths={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}, "maxItems": 20}), permission="read", path_scope="workspace")

