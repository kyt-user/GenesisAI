"""Bounded-memory text inspection and character/line ranges, independent of file size."""

from pathlib import Path

from genesisai.shared.security import ToolError


CHUNK = 64 * 1024


def metadata(path):
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(8192)
    if b"\0" in head:
        raise ToolError("unsupported_format", "目标是二进制文件，请使用对应内容处理器")
    bom = head.startswith(b"\xef\xbb\xbf")
    for encoding in (("utf-8-sig",) if bom else ("utf-8", "gb18030")):
        try:
            size = lf = cr = crlf = 0
            previous = ""
            with path.open(encoding=encoding, newline="") as stream:
                while chunk := stream.read(CHUNK):
                    size += len(chunk)
                    if '\0' in chunk:
                        raise ToolError('unsupported_format', '文本中含 NUL；请使用二进制处理器')
                    lf += chunk.count("\n")
                    cr += chunk.count("\r")
                    crlf += (previous + chunk).count("\r\n")
                    previous = chunk[-1:]
            endings = {'LF': lf - crlf, 'CRLF': crlf, 'CR': cr - crlf}
            return {"encoding": encoding, "bom": bom, "newline": max(endings, key=endings.get),
                    "total_chars": size, "total_lines": sum(endings.values()) + int(bool(size) and previous not in {'\n', '\r'})}
        except UnicodeDecodeError:
            continue
    raise ToolError("unsupported_encoding", "文件编码不受支持")


def characters(path, offset, limit, info=None):
    info = info or metadata(path)
    with Path(path).open(encoding=info["encoding"], newline="") as stream:
        left = offset
        while left:
            chunk = stream.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
        body = stream.read(limit)
    return body, info, offset + len(body) if offset + len(body) < info["total_chars"] else None


def lines(path, start, end, limit):
    info = metadata(path)
    selected, used, raw_offset, line_number = [], 0, 0, 1
    next_character = None
    with Path(path).open(encoding=info["encoding"], newline="") as stream:
        while line_number <= end:
            beginning = raw_offset
            raw = stream.readline(limit + 1)
            if not raw:
                break
            raw_offset += len(raw)
            if line_number < start:
                while not raw.endswith(("\n", "\r")):
                    raw = stream.readline(CHUNK)
                    raw_offset += len(raw)
                    if not raw:
                        break
                line_number += 1
                continue
            line = f"{line_number}: {raw.rstrip(chr(13) + chr(10))}"
            size = len(line) + bool(selected)
            if len(raw) > limit or used + size > limit:
                next_character = beginning
                break
            selected.append(line)
            used += size
            line_number += 1
    return "\n".join(selected), info, min(start + len(selected) - 1, info["total_lines"]), next_character
