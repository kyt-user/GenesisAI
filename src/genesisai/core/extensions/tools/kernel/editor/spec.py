from genesisai.core.tools.base import ToolSpec, object_schema

TEXT_REFERENCE = object_schema(['path', 'sha256'],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    sha256={'type': 'string', 'minLength': 64, 'maxLength': 64})

SPEC = ToolSpec(
    routes=('direct_answer', 'local_files', 'file_management', 'coding', 'revision', 'creative'),
    parameters=object_schema(
        ["operation"],
        operation={"type": "string", "minLength": 1, "maxLength": 20},
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        content={"type": "string", "maxLength": 100000},
        content_ref=TEXT_REFERENCE,
        source={"type": "string", "minLength": 1, "maxLength": 2000},
        destination={"type": "string", "minLength": 1, "maxLength": 2000},
        old_text={"type": "string", "minLength": 1, "maxLength": 100000},
        new_text={"type": "string", "maxLength": 100000},
        old_text_ref=TEXT_REFERENCE,
        new_text_ref=TEXT_REFERENCE,
        patches={"type": "array", "minItems": 1, "items": object_schema(
            ["path"], path={"type": "string", "minLength": 1, "maxLength": 2000},
            patch={"type": "string", "minLength": 1, "maxLength": 100000},
            patch_ref=TEXT_REFERENCE,
            expected_sha256={"type": "string", "minLength": 64, "maxLength": 64})},
        patch={"type": "string", "minLength": 1, "maxLength": 100000},
        patch_ref=TEXT_REFERENCE,
        expected_sha256={"type": "string", "minLength": 64, "maxLength": 64},
        source_refs={"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
        scope={"type": "string", "minLength": 6, "maxLength": 9},
    ),
    execution="job",
    permission="write",
    path_scope="workspace",
    side_effect=True,
    max_output_chars=4000,
)
