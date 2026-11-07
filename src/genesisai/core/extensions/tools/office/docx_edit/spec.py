from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', dependency_extra='office',
    routes=('docx',),
    parameters=object_schema(["source","path","find","replace"], source={"type": "string", "minLength": 1, "maxLength": 2000}, path={"type": "string", "minLength": 1, "maxLength": 2000}, find={"type": "string", "minLength": 1}, replace={"type": "string"}, overwrite={"type":"boolean"}, expected_sha256={"type":"string","minLength":64,"maxLength":64}), permission="write", side_effect=True, path_scope="output", dependencies=("docx",))

