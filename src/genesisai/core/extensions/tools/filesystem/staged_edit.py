"""Preflight edits into an owned temporary stream; commit only after conflict checks."""

import difflib
import hashlib
import io
from tempfile import TemporaryFile

from genesisai.shared.security import ToolError
from .patching import write_patch
from .text_ranges import metadata, CHUNK


def unique_position(path, encoding, old):
    found, base, pending = None, 0, ''
    with path.open(encoding=encoding, newline='') as source:
        while chunk := source.read(CHUNK):
            pending += chunk
            start = 0
            while (position := pending.find(old, start)) >= 0:
                absolute = base + position
                if found is not None and absolute != found:
                    raise ToolError('patch_conflict', 'old_text 必须在目标中恰好出现一次')
                found = absolute
                start = position + 1
            consumed = max(0, len(pending) - len(old) + 1)
            base += consumed
            pending = pending[consumed:]
    if found is None:
        raise ToolError('patch_conflict', 'old_text 必须在目标中恰好出现一次')
    return found


def stage(path, *, operation, patch=None, old_text=None, new_text=None):
    info = metadata(path)
    newline = {'CRLF': '\r\n', 'CR': '\r', 'LF': '\n'}[info['newline']]
    stream = TemporaryFile('w+b')
    try:
        writer = io.TextIOWrapper(stream, encoding=info['encoding'], newline='')
        with path.open(encoding=info['encoding'], newline='') as source:
            if operation == 'replace':
                old = old_text.replace('\r\n', '\n').replace('\n', newline)
                new = new_text.replace('\r\n', '\n').replace('\n', newline)
                position = unique_position(path, info['encoding'], old)
                remaining = position
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ToolError('patch_conflict', '预检时文件已改变')
                    writer.write(chunk)
                    remaining -= len(chunk)
                source.read(len(old))
                writer.write(new)
                while chunk := source.read(CHUNK):
                    writer.write(chunk)
                preview = '\n'.join(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=str(path), tofile=str(path), lineterm=''))
            else:
                write_patch(source, writer, patch, newline)
                preview = patch
        writer.flush()
        writer.detach()
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        stream.seek(0)
        return stream, digest, preview
    except BaseException:
        stream.close()
        raise
