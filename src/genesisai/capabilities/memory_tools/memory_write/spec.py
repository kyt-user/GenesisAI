from genesisai.runtime.base import ToolSpec, object_schema

SPEC = ToolSpec(parameters=object_schema(["type", "title", "summary", "content"], type={"type": "string", "enum": ["project", "architecture", "convention", "command", "decision", "issue"]}, title={"type": "string", "minLength": 1, "maxLength": 200}, summary={"type": "string", "minLength": 1, "maxLength": 500}, content={"type": "string", "minLength": 1, "maxLength": 4000}, keywords={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 30}, source_path={"type": "string", "minLength": 1, "maxLength": 2000}, verified={"type": "boolean"}), permission="write", side_effect=True)

