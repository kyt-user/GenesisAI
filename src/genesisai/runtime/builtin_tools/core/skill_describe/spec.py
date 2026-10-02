from genesisai.runtime.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["name"], name={"type": "string", "minLength": 2, "maxLength": 64}), permission="core")

