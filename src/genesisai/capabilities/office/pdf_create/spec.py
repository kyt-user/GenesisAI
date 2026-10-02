from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["path","lines"], path={"type": "string", "minLength": 1, "maxLength": 2000}, lines={"type": "array", "items": {"type": "string", "maxLength": 1000}, "minItems": 1, "maxItems": 5000}), permission="write", side_effect=True, path_scope="output", dependencies=("reportlab",))

