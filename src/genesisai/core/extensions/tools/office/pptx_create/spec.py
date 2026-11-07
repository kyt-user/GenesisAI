from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', dependency_extra='office',
    routes=('pptx',),
    parameters=object_schema(["path","slides"], path={"type": "string", "minLength": 1, "maxLength": 2000}, slides={"type": "array", "items": {"type": "object"}, "minItems": 1}, overwrite={"type":"boolean"}, expected_sha256={"type":"string","minLength":64,"maxLength":64}, source={"type":"string","minLength":1,"maxLength":2000}), permission="write", side_effect=True, path_scope="output", dependencies=("pptx",))

