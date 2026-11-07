from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(routes=('audio', '音频', 'media', 'video'), parameters=object_schema(['path', 'output'],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    output={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    start_seconds={'type': 'number', 'minimum': 0}, duration_seconds={'type': 'number', 'minimum': 0.01, 'maximum': 600},
    expected_sha256={'type': 'string', 'minLength': 64, 'maxLength': 64}),
    permission='write', path_scope='output', side_effect=True, execution='job', execution_timeout_seconds=None)
