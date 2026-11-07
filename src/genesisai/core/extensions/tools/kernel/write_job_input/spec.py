from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    routes=('jobs',), permission='write', path_scope='workspace', side_effect=True,
    timeout_seconds=35,
    parameters=object_schema(['job_id', 'action'],
        job_id={'type': 'string', 'minLength': 1},
        action={'type': 'string', 'enum': ['write', 'close', 'resize']},
        text={'type': 'string'}, secret_from_env={'type': 'string', 'minLength': 1},
        append_newline={'type': 'boolean'},
        rows={'type': 'integer', 'minimum': 1, 'maximum': 32767},
        columns={'type': 'integer', 'minimum': 1, 'maximum': 32767}),
)
