from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["query"], query={"type": "string", "maxLength": 200}, limit={"type": "integer", "minimum": 1, "maximum": 20}), permission="core")

