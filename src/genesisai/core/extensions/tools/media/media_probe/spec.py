from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(routes=('media', 'video', 'audio', '视频', '音频'), parameters=object_schema(['path'],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    expected_sha256={'type': 'string', 'minLength': 64, 'maxLength': 64}),
    permission='read', path_scope='authorized', execution='job', execution_timeout_seconds=None)
