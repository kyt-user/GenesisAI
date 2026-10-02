import socket
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Offline tests must not access network')
    monkeypatch.setattr(socket.socket, 'connect', denied)


@pytest.fixture(autouse=True)
def _local_state_root(monkeypatch):
    """Force session state into workspace/.genesis for test isolation."""
    from pathlib import Path as _Path
    from genesisai.state import store as _store_mod

    def _local_root(workspace):
        return _Path(workspace).resolve() / '.genesis'

    monkeypatch.setattr(_store_mod, 'workspace_state_root', _local_root)
    from genesisai.app import cli as _cli_mod
    monkeypatch.setattr(_cli_mod, 'workspace_state_root', _local_root)
