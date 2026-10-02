from genesisai.runtime.base import ToolSpec, object_schema

SPEC = ToolSpec(parameters=object_schema([], query={"type": "string", "maxLength": 500}, type={"type": "string", "maxLength": 30}, limit={"type": "integer", "minimum": 1, "maximum": 100}), permission="read")

