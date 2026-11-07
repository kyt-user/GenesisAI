from genesisai.core.tools.base import ToolSpec, object_schema
from genesisai.core.extensions.tools.kernel.fetch_web_content.spec import SPEC as PUBLIC

FIELDS = {'url', 'offset', 'limit', 'section', 'block', 'page', 'fields',
          'include_cross_domain', 'discovery_depth', 'refresh', 'network_limits'}
SPEC = ToolSpec(parameters=object_schema(['url'],
    **{key: value for key, value in PUBLIC.parameters['properties'].items() if key in FIELDS},
    resource_domains={'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    wait_for={'type': 'string', 'minLength': 1},
    settle_seconds={'type': 'number', 'minimum': 0},
    max_requests={'type': 'integer', 'minimum': 1},
    max_total_bytes={'type': 'integer', 'minimum': 1}),
    permission='network', network=True, execution='job', routes=('web', 'browser', '动态网页'),
    dependencies=('playwright',), dependency_extra='browser', max_output_chars=1500000)
