from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    parameters=object_schema([], path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
                             image_ref={'type': 'string', 'minLength': 1, 'maxLength': 100}),
    permission='read', path_scope='authorized', history_lookup=True,
)
