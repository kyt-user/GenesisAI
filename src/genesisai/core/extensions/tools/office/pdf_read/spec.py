from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["path"], path={"type": "string", "minLength": 1, "maxLength": 2000}), permission="read", path_scope="authorized", dependencies=("pypdf",))

