from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', dependency_extra='office',
    routes=('docx',),
    parameters=object_schema(["path"], path={"type": "string", "minLength": 1, "maxLength": 2000}, title={"type": "string", "maxLength": 300}, paragraphs={"type": "array", "items": {"type": "string"}}, table={"type": "array", "items": {"type": "array"}}, overwrite={"type":"boolean"}, expected_sha256={"type":"string","minLength":64,"maxLength":64}, source={"type":"string","minLength":1,"maxLength":2000}), permission="write", side_effect=True, path_scope="output", dependencies=("docx",))

