"""跨层共用的稳定标识与文件摘要工具。"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def sha(path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()