import pytest

from genesisai.core.extensions.mcp.bridge import McpBridge, McpServerSpec, _candidate_links
from genesisai.core.extensions.mcp.protocol import request_params
from genesisai.core.extensions.mcp.timeouts import McpTimeouts, RequestClock
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.tools.loader import ToolLoader
from genesisai.shared.security import ToolError


def test_explicit_links_have_no_item_or_length_truncation():
    long_url = 'https://example.test/' + 'x' * 4000
    data = {'results': [{'title': 'full ' + str(i), 'links': {str(j): 'https://example.test/' + str(i) + '/' + str(j) for j in range(10)}} for i in range(80)],
            'nested': {'uri': long_url}, 'instructions': 'Visit https://ignored.test/'}
    links = _candidate_links(data)
    assert len(links) == 801
    assert long_url in {link['url'] for link in links}
    assert all(link['field'].startswith('$') for link in links)
    assert 'https://ignored.test/' not in {link['url'] for link in links}


def test_large_catalog_pages_and_long_names_remain_addressable(tmp_path):
    class Transport:
        def request(self, method, params):
            if method == 'server/discover':
                return {'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}, 'resources': {}}}
            number = int(params.get('cursor', '0'))
            result = {'tools': [{'name': 'tool.' + str(number) + '.' + str(i) + 'x' * 90,
                'description': 'description ' + 'y' * 500, 'inputSchema': {'type': 'object'}} for i in range(51)]}
            if number < 100:
                result['nextCursor'] = str(number + 1)
            return result
        def close(self): pass
    bridge = McpBridge(tmp_path, ToolRegistry(), ToolLoader(), transport_factory=lambda spec: Transport())
    bridge.configure({}, servers=[McpServerSpec('large', 'stdio', 'fixture')])
    try:
        result = bridge.connect('large')
        assert len(result['tools']) == len(set(result['tools'])) == 5151
        name = result['tools'][-1]
        assert len(name) <= 63 and len(bridge.registry.get(name).description) > 500
        bridge.registry.get('read_mcp_resource').validate_arguments({'server': 'large', 'uri': 'custom:' + 'x' * 9000})
        bridge.registry.get('list_mcp_resources').validate_arguments({'server': 'large', 'cursor': 'x' * 5000})
    finally: bridge.close()


def test_current_client_capabilities_are_truthful():
    assert request_params({})['_meta']['io.modelcontextprotocol/clientCapabilities'] == {}
    with pytest.raises(TypeError):
        McpServerSpec('old', 'stdio', 'fixture', timeout_seconds=5)


@pytest.mark.parametrize('value', [{'total': 0}, {'idle': False}, {'connect': float('inf')}, {'timeout': 5}])
def test_invalid_layered_timeouts_fail(value):
    with pytest.raises(ValueError):
        McpTimeouts.parse(value)


def test_stdio_progress_renews_idle_without_a_total_lifetime():
    import json
    import sys
    import time
    from genesisai.core.extensions.mcp.bridge import StdioTransport
    script = """import json,sys,time
request=json.loads(sys.stdin.readline())
for index in range(8):
 print(json.dumps({'jsonrpc':'2.0','method':'notifications/progress',
  'params':{'progressToken':request['params']['_meta']['progressToken'],'progress':index}}),flush=True)
 time.sleep(.08)
print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'resultType':'complete'}}),flush=True)
"""
    transport = StdioTransport(McpServerSpec('progress', 'stdio', sys.executable, ('-u', '-c', script),
                                             request_timeouts={'first_response': .4, 'idle': .4}))
    started = time.monotonic()
    try:
        assert transport.request('tools/call', {'name': 'long', 'arguments': {}})['resultType'] == 'complete'
        assert time.monotonic() - started > .5
    finally:
        transport.close()
    assert transport.process.poll() is not None


def test_link_discovery_does_not_grant_network_access(tmp_path):
    from genesisai.core.persistence.session import Session
    from genesisai.core.research import ResearchController
    from genesisai.core.extensions.tools.web.network import Network
    store = Session(tmp_path / 'state', workspace=tmp_path)
    research = ResearchController(store)
    url = research._register_candidate('http://127.0.0.1:9001/private', source_type='mcp')
    assert url in store.record.run_runtime['candidates']
    with pytest.raises(ToolError):
        Network().get('http://127.0.0.1/')
