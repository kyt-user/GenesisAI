from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["path"], path={"type": "string", "minLength": 1, "maxLength": 2000}, title={"type": "string", "maxLength": 300}, paragraphs={"type": "array", "items": {"type": "string", "maxLength": 5000}, "maxItems": 1000}, table={"type": "array", "items": {"type": "array"}, "maxItems": 500}), permission="write", side_effect=True, path_scope="output", dependencies=("docx",))

