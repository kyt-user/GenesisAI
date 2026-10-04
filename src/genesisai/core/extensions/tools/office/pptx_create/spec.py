from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["path","slides"], path={"type": "string", "minLength": 1, "maxLength": 2000}, slides={"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 100}), permission="write", side_effect=True, path_scope="output", dependencies=("pptx",))

