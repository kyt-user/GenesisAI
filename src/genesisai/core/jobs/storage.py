"""Atomic job records, cross-process executor slots, and segmented log access."""

import errno
import json
import os
from pathlib import Path
import tempfile
import time


ACTIVE = {"queued", "running", "stopping"}
SEGMENT_BYTES = 1024 * 1024


def load(path):
    """Read a published snapshot, tolerating transient Windows sharing locks."""
    deadline = time.monotonic() + 5
    while True:
        try:
            with Path(path).open(encoding='utf-8') as stream:
                return json.load(stream)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def save(path, data, *, max_bytes=None):
    path = Path(path)
    # Keep temporary names independent of long target names and nested job paths.
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='', suffix='.tmp')
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            size = 0
            for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(data):
                encoded = part.encode('utf-8')
                size += len(encoded)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError('结构化结果达到配置的存储配额')
                stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)  # Only this invocation's unpublished temporary file.
        raise
    # Windows readers/virus scanners may briefly hold a non-delete-sharing
    # handle. Retry the atomic rename, never the operation whose state we save.
    deadline = time.monotonic() + 5
    while True:
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)


class SlotBusy(BlockingIOError):
    """A live owner holds this slot; filesystem failures are not contention."""


class Slot:
    def __init__(self, path):
        self.stream = Path(path).open("a+b")
        try:
            self.stream.seek(0, 2)
            if not self.stream.tell():
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise SlotBusy(exc.errno, "作业槽正由其他执行器使用", str(path)) from exc
                raise
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()


class LogWriter:
    def __init__(self, root, quota):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = quota
        self.size = 0

    def write(self, text):
        value = text.encode("utf-8")
        if self.size + len(value) > self.quota:
            raise ValueError("作业日志达到配置存储配额")
        while value:
            index, offset = divmod(self.size, SEGMENT_BYTES)
            chunk, value = value[:SEGMENT_BYTES - offset], value[SEGMENT_BYTES - offset:]
            with (self.root / f"{index:08d}.log").open("ab") as stream:
                stream.write(chunk)
                stream.flush()
            self.size += len(chunk)


def read_log(root, offset, limit):
    root = Path(root)
    chunks, remaining, cursor = [], limit, offset
    while remaining:
        index, position = divmod(cursor, SEGMENT_BYTES)
        path = root / f"{index:08d}.log"
        if not path.is_file():
            break
        with path.open("rb") as stream:
            stream.seek(position)
            chunk = stream.read(min(remaining, SEGMENT_BYTES - position))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        cursor += len(chunk)
    return b"".join(chunks)


def log_size(root):
    return sum(path.stat().st_size for path in Path(root).glob("*.log"))
