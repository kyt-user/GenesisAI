"""按会话追加的完整 I/O 日志；每个事件独立落盘，不受上下文压缩影响。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import traceback
import hashlib
from uuid import uuid4


_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|credential|access[_-]?token|refresh[_-]?token|qwen_token_plan)", re.I)
_ASSIGNMENT = re.compile(
    r'''(?i)((?:api[_-]?key|password|secret|access[_-]?token|refresh[_-]?token|qwen_token_plan)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'[^']*'|[^\s,"'\\}]+)'''
)
_BEARER = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")


class IOJournal:
    def __init__(self, store):
        self.store = store
        self.path = store.root / "logs" / f"{store.id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._secrets = set()
        for key, value in os.environ.items():
            if _SECRET_KEY.search(key) and len(value) >= 8:
                self.add_secret(value)

    def add_secret(self, value):
        if isinstance(value, str) and value:
            self._secrets.add(value)

    def clean(self, value):
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, dict):
            if isinstance(value.get('data'), bytes):
                value = {**value, 'data': {'sha256': hashlib.sha256(value['data']).hexdigest(), 'byte_size': len(value['data'])}}
            return {str(k): "[REDACTED]" if _SECRET_KEY.fullmatch(str(k)) and isinstance(v, str) else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            value = re.sub(r'data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+',
                           lambda m: '[图片载荷 sha256=' + hashlib.sha256(m[0].encode()).hexdigest() + ']', value)
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = _BEARER.sub(r"\1[REDACTED]", value)
            return _ASSIGNMENT.sub(r"\1[REDACTED]", value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.clean(str(value))

    def write(self, event, *, round_num=None, event_run_id=..., **payload):
        record = self.clean({
            "schema_version": 1,
            "event_id": uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.store.id,
            "run_id": self.store.data.get("run_id") if event_run_id is ... else event_run_id,
            "round_num": self.store.data.get("rounds", 0) if round_num is None else round_num,
            "event": event,
            "payload": payload,
        })
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        return record["event_id"]

    @staticmethod
    def exception(exc):
        return {
            "type": type(exc).__name__, "message": str(exc),
            "status_code": getattr(exc, "status_code", None),
            "request_id": getattr(exc, "request_id", None),
            "body": getattr(exc, "body", None),
            "frames": [{"file": f.filename, "function": f.name, "line": f.lineno}
                       for f in traceback.extract_tb(exc.__traceback__)],
        }
