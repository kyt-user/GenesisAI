"""Only genuine lock contention is retryable queue pressure."""
import errno
import io
import os
from pathlib import Path

import pytest

from genesisai.core.jobs.storage import Slot, SlotBusy


class Stream(io.BytesIO):
    def fileno(self):
        return 123


def patch_lock(monkeypatch, failure):
    def fail(*args):
        raise failure
    if os.name == 'nt':
        import msvcrt
        monkeypatch.setattr(msvcrt, 'locking', fail)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, 'flock', fail)


@pytest.mark.parametrize('code', [errno.EACCES, errno.EAGAIN])
def test_lock_contention_has_dedicated_type_and_closes_stream(tmp_path, monkeypatch, code):
    stream = Stream()
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: stream)
    patch_lock(monkeypatch, OSError(code, 'locked'))
    with pytest.raises(SlotBusy) as failure:
        Slot(tmp_path / 'slot.lock')
    assert failure.value.errno == code and stream.closed


def test_open_permission_error_is_not_contention(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, 'filesystem denied')
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(PermissionError) as failure:
        Slot(tmp_path / 'slot.lock')
    assert not isinstance(failure.value, SlotBusy)


def test_initialization_write_failure_closes_stream(tmp_path, monkeypatch):
    class Full(Stream):
        def write(self, *args):
            raise OSError(errno.ENOSPC, 'full')
    stream = Full()
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: stream)
    with pytest.raises(OSError) as failure:
        Slot(tmp_path / 'slot.lock')
    assert failure.value.errno == errno.ENOSPC
    assert not isinstance(failure.value, SlotBusy) and stream.closed


def test_other_lock_failure_propagates_and_closes_stream(tmp_path, monkeypatch):
    stream = Stream()
    monkeypatch.setattr(Path, 'open', lambda *args, **kwargs: stream)
    patch_lock(monkeypatch, OSError(errno.EBADF, 'invalid descriptor'))
    with pytest.raises(OSError) as failure:
        Slot(tmp_path / 'slot.lock')
    assert failure.value.errno == errno.EBADF
    assert not isinstance(failure.value, SlotBusy) and stream.closed
