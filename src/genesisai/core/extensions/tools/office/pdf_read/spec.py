from genesisai.core.tools.base import ToolSpec, object_schema
SPEC = ToolSpec(execution='job', dependency_extra='office',routes=('pdf',), parameters=object_schema(['path'],
    path={'type':'string','minLength':1,'maxLength':2000},
    offset={'type':'integer','minimum':0}, text_offset={'type':'integer','minimum':0},
    limit={'type':'integer','minimum':1,'maximum':1000}, max_chars={'type':'integer','minimum':1,'maximum':200000},
    expected_sha256={'type':'string','minLength':64,'maxLength':64}),
    permission='read', path_scope='authorized', dependencies=('pypdf',), max_output_chars=300000)
