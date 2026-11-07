"""Resolve only verified binary resources already owned by the current session."""
import json
import re
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError


def downloaded_document(context, reference, expected):
    if not isinstance(reference, str) or not re.fullmatch(r'src_[a-f0-9]+', reference):
        raise ToolError('validation_error', 'source_ref 必须是完整来源引用')
    source = context.store.record.sources.get(reference)
    if not source or source.get('kind') not in {'web', 'private_web', 'web_render'}:
        raise ToolError('permission_denied', '来源不属于当前会话的已下载资源')
    root = context.store.root / 'sources'
    metadata = root / (reference + '.json')
    path = root / (reference + '.bin')
    for item in (metadata, path):
        if item.resolve() != item.absolute() or not item.is_file():
            raise ToolError('source_integrity', '下载来源缺失或已被链接替换')
    page = json.loads(metadata.read_text(encoding='utf-8'))
    raw = page.get('raw_source') or {}
    if (raw.get('source_ref') != reference or raw.get('bytes') != path.stat().st_size
            or raw.get('sha256') != sha(path)):
        raise ToolError('source_integrity', '下载来源长度或哈希不匹配')
    from genesisai.shared.filetypes import detect_file_type
    if detect_file_type(path) != expected:
        raise ToolError('format_mismatch', '下载内容不是 ' + expected)
    return path, reference
