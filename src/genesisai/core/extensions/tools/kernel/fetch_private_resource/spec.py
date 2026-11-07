from genesisai.core.tools.base import ToolSpec, object_schema
from genesisai.core.extensions.tools.kernel.fetch_web_content.spec import SPEC as PUBLIC


FIELDS = {'url', 'offset', 'limit', 'section', 'block', 'page', 'fields', 'include_cross_domain',
          'discovery_depth', 'refresh', 'network_limits'}
SPEC = ToolSpec(
    routes=('direct_answer', 'web'),
    parameters=object_schema(['url', 'address_ranges'],
        **{key: value for key, value in PUBLIC.parameters['properties'].items() if key in FIELDS},
        address_ranges={'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}}),
    permission='network', network=True, network_boundary='private', path_scope='none',
    execution='job', timeout_seconds=30, max_output_chars=1500000,
)
