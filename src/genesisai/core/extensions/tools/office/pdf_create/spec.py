from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', dependency_extra='office',
    routes=('pdf',),
    parameters=object_schema(["path","lines"], path={"type": "string", "minLength": 1, "maxLength": 2000}, lines={"type": "array", "items": {"type": "string"}, "minItems": 1}, overwrite={"type":"boolean"}, expected_sha256={"type":"string","minLength":64,"maxLength":64}), permission="write", side_effect=True, path_scope="output", dependencies=("reportlab",))

