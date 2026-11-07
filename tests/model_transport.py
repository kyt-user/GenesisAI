"""Exercise native SDK calls with mocked HTTP and loopback-only asyncio plumbing."""

import _socket
import socket

import httpx


def mock_model_http(monkeypatch, handler):
    import genesisai.model.openai_compatible as compatible

    def local_only(sock, address):
        assert address[0] in ('127.0.0.1', '::1'), 'Offline model tests must not access the internet'
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', local_only)
    monkeypatch.setattr(compatible, 'DefaultAsyncHttpxClient',
                        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
