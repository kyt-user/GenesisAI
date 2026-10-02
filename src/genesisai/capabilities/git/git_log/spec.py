from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema([], cwd={"type": "string", "minLength": 1, "maxLength": 2000}, limit={"type": "integer", "minimum": 1, "maximum": 50}), permission="read", path_scope="workspace")

