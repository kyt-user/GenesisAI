from genesisai.runtime.base import ToolSpec, object_schema

SPEC = ToolSpec(parameters=object_schema([]), permission="write", side_effect=True)

