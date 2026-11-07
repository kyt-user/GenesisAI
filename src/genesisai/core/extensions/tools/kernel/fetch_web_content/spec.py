from genesisai.core.tools.base import ToolSpec, object_schema
from genesisai.core.extensions.tools.web.contracts import MAX_QUERY_CHARS
from genesisai.core.extensions.tools.web.budget import NetworkLimits


SPEC = ToolSpec(
    routes=('direct_answer', 'web'),
    parameters=object_schema(
        [],
        query={"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        url={"type": "string", "minLength": 1},
        offset={"type": "integer", "minimum": 0},
        limit={"type": "integer", "minimum": 1, "maximum": 200000},
        section={'type': 'string', 'enum': ['text', 'source', 'links', 'structured', 'structured_raw', 'document_pages', 'page']},
        block={'type': 'integer', 'minimum': 0}, page={'type': 'integer', 'minimum': 1},
        fields={'type': 'array', 'items': {'type': 'string', 'enum': ['Product', 'Price', 'Low price', 'High price', 'Availability']}},
        include_cross_domain={'type': 'boolean'}, discovery_depth={'type': 'integer', 'minimum': 0},
        refresh={'type': 'boolean'}, result_count={'type': 'integer', 'minimum': 1, 'maximum': 100},
        provider={'type': 'string', 'minLength': 1}, language={'type': 'string', 'minLength': 1, 'maxLength': 20},
        country={'type': 'string', 'minLength': 2, 'maxLength': 20}, freshness={'type': 'string', 'minLength': 1, 'maxLength': 50},
        allowed_domains={'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 253}},
        network_limits=object_schema([], **{name: {'type': 'integer' if name in {'response_bytes', 'decoded_bytes', 'max_redirects'} else 'number', 'minimum': 0 if name == 'max_redirects' else .001}
                                          for name in NetworkLimits.__dataclass_fields__}),
    ),
    permission="network",
    network=True,
    path_scope="none",
    execution='job',
    timeout_seconds=30,
    max_output_chars=1500000,
)
