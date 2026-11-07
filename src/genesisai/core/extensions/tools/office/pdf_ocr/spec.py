from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(execution='job', dependency_extra='ocr', routes=('pdf', 'ocr', '扫描件'), parameters=object_schema([],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    source_ref={'type': 'string', 'pattern': '^src_[a-f0-9]+$'},
    offset={'type': 'integer', 'minimum': 0}, text_offset={'type': 'integer', 'minimum': 0},
    limit={'type': 'integer', 'minimum': 1, 'maximum': 10},
    dpi={'type': 'integer', 'minimum': 72, 'maximum': 300},
    max_chars={'type': 'integer', 'minimum': 1, 'maximum': 200000},
    expected_sha256={'type': 'string', 'minLength': 64, 'maxLength': 64}),
    permission='read', path_scope='authorized', dependencies=('pypdf', 'pypdfium2', 'rapidocr', 'onnxruntime'),
    max_output_chars=300000)
