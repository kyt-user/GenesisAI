from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(routes=('video', '视频', 'media'), parameters=object_schema(['path', 'output_prefix'],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    output_prefix={'type': 'string', 'minLength': 1, 'maxLength': 1900},
    start_seconds={'type': 'number', 'minimum': 0}, interval_seconds={'type': 'number', 'minimum': 0.01},
    count={'type': 'integer', 'minimum': 1, 'maximum': 16},
    expected_sha256={'type': 'string', 'minLength': 64, 'maxLength': 64}),
    permission='write', path_scope='output', side_effect=True, execution='job', execution_timeout_seconds=None)
