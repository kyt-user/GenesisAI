"""Real local HTTP reads require explicit private-network authorization."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
import time

import pytest

from genesisai.agent.evidence import EvidenceBundle
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.private_network import PrivateNetwork
from genesisai.shared.security import ToolError
from test_scoped_authorizations import runtime, grant, invoke


@pytest.fixture
def endpoint(monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', 'http://localhost:' + str(self.server.server_port) + '/private')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'PRIVATE_DOCUMENT_7319')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    import _socket
    def local_only(sock, address):
        assert address == ('127.0.0.1', server.server_port)
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', local_only)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def completed(runtime, result):
    assert result['ok'], result
    if not result['data'].get('job_id'):
        return result['data']
    identity = result['data']['job_id']
    result = invoke(runtime, 'read_job', {'job_id': identity, 'wait_seconds': 20}, 'collect_' + str(time.monotonic_ns()))
    assert result['ok'], result
    assert result['data']['status'] == 'succeeded', result
    return result['data']['result']


def test_private_requires_grant_even_auto_and_cache_does_not_bypass_revocation(runtime, endpoint):
    url, requests = endpoint
    runtime.set_permission('auto')
    args = {'url': url + '/private', 'address_ranges': ['127.0.0.1/32']}
    assert invoke(runtime, 'fetch_private_resource', args, 'before_grant')['pending']
    assert requests == []
    authorization = grant(runtime, scope='session', tools=['fetch_private_resource'],
                          domains=['127.0.0.1'], address_ranges=['127.0.0.1/32'])
    first = completed(runtime, invoke(runtime, 'fetch_private_resource', args, 'first'))
    assert first['text'] == 'PRIVATE_DOCUMENT_7319'
    ref = first['source_refs'][0]
    assert runtime.store.record.sources[ref]['kind'] == 'private_web'
    assert runtime.store.record.run_runtime['evidence_refs'] == [ref]
    EvidenceBundle(runtime.store).verify()
    second = completed(runtime, invoke(runtime, 'fetch_private_resource', args, 'cached'))
    assert second['cached'] and requests == ['/private']
    assert runtime.store.record.run_runtime['usage']['search_fetches'] == 1
    from genesisai.app.authorization_commands import execute_authorization_command
    execute_authorization_command(runtime, '/revoke ' + authorization['id'])
    assert invoke(runtime, 'fetch_private_resource', args, 'revoked')['pending']
    assert requests == ['/private']


def test_private_one_shot_and_public_boundary(runtime, endpoint):
    url, requests = endpoint
    args = {'url': url + '/private', 'address_ranges': ['127.0.0.1/32']}
    pending = invoke(runtime, 'fetch_private_resource', args, 'confirm')
    assert pending['pending']
    result = invoke(runtime, 'fetch_private_resource', args, 'confirm', runtime.store.record.calls['confirm']['digest'])
    assert completed(runtime, result)['text'] == 'PRIVATE_DOCUMENT_7319'
    with pytest.raises(ToolError):
        Network().get('http://127.0.0.1/')
    network = PrivateNetwork(permit={'domains': ['127.0.0.1'], 'address_ranges': ['127.0.0.1/32']})
    with pytest.raises(ToolError, match='范围授权'):
        network.get(url + '/redirect')
    assert requests == ['/private', '/redirect']


def test_private_rejects_mixed_dns_and_special_addresses():
    addresses = ['127.0.0.1', '169.254.169.254']
    resolver = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (value, 80)) for value in addresses]
    network = PrivateNetwork(permit={'domains': ['fixture.test'], 'address_ranges': ['0.0.0.0/0']}, resolver=resolver,
                             transport=lambda *a: pytest.fail('must reject before transport'))
    with pytest.raises(ToolError, match='CIDR'):
        network.get('http://fixture.test/')
