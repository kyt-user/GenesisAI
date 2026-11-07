"""参考 xiaoerAI 文件格式模块实现的有界文档提取。"""
import csv
import io
import json
import os
import zipfile
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.shared.filetypes import TEXT_SUFFIXES

SUFFIXES = {*TEXT_SUFFIXES, '.docx', '.xlsx', '.pptx', '.pdf'}


def read_text_document(path):
    """读取普通文本并返回内容、实际编码、主换行和 BOM 状态。"""
    path = Path(path)
    raw = path.read_bytes()
    if b'\x00' in raw:
        raise ToolError('unsupported_format', '目标是二进制文件，不能作为文本修改')
    bom = raw.startswith(b'\xef\xbb\xbf')
    encoding = 'utf-8-sig' if bom else 'utf-8'
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = 'gb18030'
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ToolError('unsupported_encoding', '文件编码不受支持') from exc
    crlf = text.count('\r\n')
    lf = text.count('\n') - crlf
    newline = '\r\n' if crlf > lf else '\n'
    return text, encoding, newline, bom


def encode_text_document(text, encoding, bom):
    target_encoding = 'utf-8' if encoding == 'utf-8-sig' else encoding
    payload = text.encode(target_encoding)
    if bom and target_encoding == 'utf-8':
        payload = b'\xef\xbb\xbf' + payload
    return payload


def extract(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        try:
            text, _, _, _ = read_text_document(path)
        except ToolError as exc:
            raise ToolError('unsupported_format', '不支持的格式') from exc
        return text, False
    truncated = False
    if suffix in TEXT_SUFFIXES:
        text, _, _, _ = read_text_document(path)
        if suffix == '.json':
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        elif suffix == '.csv':
            text = '\n'.join('\t'.join(row) for row in csv.reader(io.StringIO(text)))
    else:
        from genesisai.core.extensions.tools.office.readers import iter_text
        text = ''.join(iter_text(path, suffix[1:]))
    if not text.strip():
        raise ToolError('no_text', '资料没有可读正文')
    return text, truncated


def walk(access, path, cap=None, *, include=()):
    root = access.read(path)
    count = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [f for f in sorted(folders) if (f not in {'.git', '.venv', 'node_modules', '__pycache__'} or f in include) and not Path(directory, f).is_symlink() and not bool(getattr(Path(directory, f).lstat(), 'st_file_attributes', 0) & 0x400)]
        for name in sorted(files):
            count += 1
            if cap is not None and count > cap:
                return
            candidate = Path(directory, name)
            try:
                yield access.read(str(candidate))
            except (ToolError, OSError):
                continue

