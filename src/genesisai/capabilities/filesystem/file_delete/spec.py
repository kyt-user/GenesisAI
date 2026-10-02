from genesisai.runtime.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        expected_sha256={"type": "string", "minLength": 64, "maxLength": 64},
    ),
    permission="write", path_scope="workspace", side_effect=True,
)

