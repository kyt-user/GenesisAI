from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('creative', 'creative_writing'),
    parameters=object_schema(
        ["path"],
        path={"type": "string", "minLength": 1, "maxLength": 2000},
        offset={'type':'integer','minimum':0}, limit={'type':'integer','minimum':1,'maximum':50000},
        expected_sha256={'type':'string','minLength':64,'maxLength':64},
        group={'type':'string','minLength':1,'maxLength':2000},
        fact_offset={'type':'integer','minimum':0}, fact_limit={'type':'integer','minimum':1,'maximum':1000},
    ),
    max_output_chars=200000,
    permission="read",
    path_scope="authorized",
)
