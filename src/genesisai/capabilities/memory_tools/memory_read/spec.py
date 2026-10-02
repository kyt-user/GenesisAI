from genesisai.runtime.base import ToolSpec, object_schema

SPEC = ToolSpec(parameters=object_schema(["id"], id={"type": "string", "minLength": 5, "maxLength": 80}), permission="read")

