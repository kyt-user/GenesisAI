"""以文件签名和扩展名共同识别受支持格式。"""

from pathlib import Path
import os
import zipfile

from genesisai.shared.security import ToolError


TEXT_SUFFIXES = {
    '.txt', '.md', '.markdown', '.csv', '.json', '.yaml', '.yml', '.toml', '.xml',
    '.py', '.pyi', '.js', '.jsx', '.ts', '.tsx', '.java', '.kt', '.kts', '.c', '.h',
    '.cpp', '.hpp', '.cs', '.go', '.rs', '.rb', '.php', '.sh', '.ps1', '.bat', '.cmd',
    '.ini', '.cfg', '.conf', '.sql', '.html', '.css', '.scss', '.vue',
}


def inspect_ooxml(path):
    """One resource budget for every OOXML entry point; never extract to disk."""
    def setting(name, default):
        try:
            value = int(os.environ.get('GENESISAI_DOCUMENT_' + name, default))
        except ValueError as exc:
            raise ToolError('validation_error', '文档资源配额必须为正整数') from exc
        if value < 1:
            raise ToolError('validation_error', '文档资源配额必须为正整数')
        return value
    expanded_limit = setting('MAX_EXPANDED_BYTES', 256 * 1024 * 1024)
    entry_limit = setting('MAX_ENTRIES', 100000)
    ratio_limit = setting('MAX_COMPRESSION_RATIO', 2000)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len({item.filename for item in members}) != len(members):
                raise ToolError('corrupt_document', 'OOXML 容器含重复成员名')
            if len(members) > entry_limit or sum(item.file_size for item in members) > expanded_limit:
                raise ToolError('size_limit', 'OOXML 超过统一解包资源配额')
            if any(item.file_size > 1024 * 1024 and item.file_size > max(1, item.compress_size) * ratio_limit for item in members):
                raise ToolError('size_limit', 'OOXML 压缩倍率超过安全配额')
            if any(item.flag_bits & 1 for item in members):
                raise ToolError('encrypted', '不支持加密的 OOXML 容器')
            return {item.filename for item in members}
    except zipfile.BadZipFile as exc:
        raise ToolError('corrupt_document', 'ZIP/OOXML 容器损坏') from exc


def detect_file_type(path):
    path = Path(path)
    with path.open("rb") as stream:
        head = stream.read(8192)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        names = inspect_ooxml(path)
        if "word/document.xml" in names: return "docx"
        if "xl/workbook.xml" in names: return "xlsx"
        if "ppt/presentation.xml" in names: return "pptx"
        return "zip"
    if b"\x00" not in head:
        import codecs
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                codecs.getincrementaldecoder(encoding)().decode(head, final=False)
                return "text"
            except UnicodeDecodeError:
                continue
    return "binary"

