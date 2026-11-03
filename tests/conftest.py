import socket
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Offline tests must not access network')
    monkeypatch.setattr(socket.socket, 'connect', denied)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Exercise real path resolution without touching the user's history."""
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, 'home', classmethod(lambda cls: tmp_path / 'home'))


@pytest.fixture(autouse=True)
def _isolated_model_config(tmp_path, monkeypatch):
    """Keep CLI-managed model config out of the developer's real user directory."""
    monkeypatch.setenv('GENESISAI_CONFIG_DIR', str(tmp_path / 'genesisai-config'))
