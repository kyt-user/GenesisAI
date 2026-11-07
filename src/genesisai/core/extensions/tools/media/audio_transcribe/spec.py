from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', execution_timeout_seconds=None, dependency_extra='speech',
    routes=('audio', 'speech', 'transcribe', '转写', '语音'), parameters=object_schema(['path'],
    path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
    start_seconds={'type': 'number', 'minimum': 0}, duration_seconds={'type': 'number', 'minimum': .01, 'maximum': 600},
    segment_offset={'type': 'integer', 'minimum': 0}, text_offset={'type': 'integer', 'minimum': 0},
    max_chars={'type': 'integer', 'minimum': 1, 'maximum': 200000},
    language={'type': 'string', 'minLength': 2, 'maxLength': 10},
    expected_sha256={'type': 'string', 'minLength': 64, 'maxLength': 64}),
    permission='read', path_scope='authorized', dependencies=('faster_whisper',), max_output_chars=300000)
