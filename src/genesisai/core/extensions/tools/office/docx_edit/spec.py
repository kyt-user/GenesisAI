from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["source","path","find","replace"], source={"type": "string", "minLength": 1, "maxLength": 2000}, path={"type": "string", "minLength": 1, "maxLength": 2000}, find={"type": "string", "minLength": 1, "maxLength": 2000}, replace={"type": "string", "maxLength": 5000}), permission="write", side_effect=True, path_scope="output", dependencies=("docx",))

