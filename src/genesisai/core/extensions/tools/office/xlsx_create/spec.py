from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(parameters=object_schema(["path","sheets"], path={"type": "string", "minLength": 1, "maxLength": 2000}, sheets={"type": "object"}), permission="write", side_effect=True, path_scope="output", dependencies=("openpyxl",))

