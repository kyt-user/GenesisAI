from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(
    routes=('memory',),
    parameters=object_schema(['operation'],
        operation={'type': 'string', 'enum': ['status', 'refresh', 'rebuild', 'cancel']},
        maintenance_id={'type': 'string', 'minLength': 1},
        offset={'type': 'integer', 'minimum': 0},
        limit={'type': 'integer', 'minimum': 1, 'maximum': 1000},
        wait_seconds={'type': 'number', 'minimum': 0, 'maximum': 30}),
    permission='core', history_lookup=True, max_output_chars=24000)
