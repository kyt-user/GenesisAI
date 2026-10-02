"""以文件签名和扩展名共同识别受支持格式。"""

from pathlib import Path
import zipfile

from genesisai.shared.security import ToolError
from genesisai.capabilities.filesystem.shared import TEXT_SUFFIXES


def detect_file_type(path):
    path = Path(path)
    head = path.read_bytes()[:8192]
    suffix = path.suffix.casefold()
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                expanded = sum(item.file_size for item in archive.infolist())
                if expanded > 64 * 1024 * 1024 or len(names) > 5000:
                    raise ToolError("size_limit", "OOXML 解压规模超过安全上限")
                if "word/document.xml" in names: return "docx"
                if "xl/workbook.xml" in names: return "xlsx"
                if "ppt/presentation.xml" in names: return "pptx"
        except zipfile.BadZipFile as exc:
            raise ToolError("corrupt_document", "ZIP/OOXML 容器损坏") from exc
        return "zip"
    if b"\x00" not in head and (suffix in TEXT_SUFFIXES or not suffix):
        return "text"
    return "binary"

