from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["source","path","cells"], source={"type": "string", "minLength": 1, "maxLength": 2000}, path={"type": "string", "minLength": 1, "maxLength": 2000}, cells={"type": "object"}), permission="write", side_effect=True, path_scope="output", dependencies=("openpyxl",))

