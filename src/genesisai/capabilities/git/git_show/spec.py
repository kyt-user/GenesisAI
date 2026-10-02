from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["revision"], revision={"type": "string", "minLength": 1, "maxLength": 200}, cwd={"type": "string", "minLength": 1, "maxLength": 2000}), permission="read", path_scope="workspace")

