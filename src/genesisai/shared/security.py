"""解析资源访问边界，并为写入操作提供显式确认。"""
import hashlib
import json
import os
from pathlib import Path


class ToolError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.error_type = code
        self.retryable = retryable


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_name(path):
    # Windows 备用数据流和设备名不属于普通输出文件。
    candidate = Path(path)
    for part in candidate.parts:
        if part == candidate.anchor:
            continue
        stem = part.split('.')[0].upper()
        if ':' in part or part.endswith((' ', '.')) or stem in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise ToolError('invalid_path', '不支持设备路径、ADS 或歧义文件名')


class Access:
    def __init__(self, roots, output, state_root, *, workspace_root=None):
        self.roots = [Path(p).resolve(strict=True) for p in roots]
        self.output = Path(output).resolve()
        self.state_root = Path(state_root).resolve()
        self._protected_state = self.state_root.name.casefold() == '.genesis'
        self.workspace = Path(workspace_root or (self.roots[0] if self.roots and self.roots[0].is_dir() else self.output)).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        if self.output == self.state_root:
            raise ValueError('输出目录不能等于程序状态目录')

    def read(self, value):
        raw = Path(value)
        if not raw.is_absolute():
            if not self.roots:
                raise ToolError('permission_denied', '没有授权输入目录')
            input_root = self.roots[0] if self.roots[0].is_dir() else self.roots[0].parent
            candidates = [input_root / raw, self.output / raw]
            if raw.parts and raw.parts[0].casefold() == self.output.name.casefold() and len(raw.parts) > 1:
                candidates.append(self.output.joinpath(*raw.parts[1:]))
            raw = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        path = raw.resolve(strict=True)
        if self._protected_state and (path == self.state_root or path.is_relative_to(self.state_root)):
            raise ToolError('permission_denied', '不能读取程序状态文件')
        if path.name == '.env' or path.name.startswith('.env.'):
            raise ToolError('permission_denied', '不向工具开放密钥文件')
        if not any(path == root or path.is_relative_to(root) for root in [*self.roots, self.output]):
            raise ToolError('permission_denied', '路径超出授权范围')
        return path

    def workspace_path(self, value, *, must_exist=False, allow_directory=False):
        raw = Path(value)
        target = raw.resolve(strict=must_exist) if raw.is_absolute() else (self.workspace / raw).resolve(strict=must_exist)
        safe_name(target)
        if not target.is_relative_to(self.workspace) or (target == self.workspace and not allow_directory):
            raise ToolError('permission_denied', '路径超出工作区')
        if self._protected_state and (target == self.state_root or target.is_relative_to(self.state_root)):
            raise ToolError('permission_denied', '不能操作程序状态文件')
        if target.name == '.env' or target.name.startswith('.env.'):
            raise ToolError('permission_denied', '不能操作密钥文件')
        if must_exist and target.is_dir() and not allow_directory:
            raise ToolError('invalid_path', '目标必须是文件')
        return target

    def write(self, value):
        relative = Path(value)
        if relative.is_absolute() or '..' in relative.parts:
            raise ToolError('permission_denied', '输出只能使用输出目录内的相对路径')
        safe_name(relative)
        target = (self.output / relative).resolve()
        if target == self.output or not target.is_relative_to(self.output):
            raise ToolError('permission_denied', '输出路径越界')
        if self._protected_state and (target == self.state_root or target.is_relative_to(self.state_root)):
            raise ToolError('permission_denied', '不能操作程序状态文件')
        if target.name == '.env' or target.name.startswith('.env.'):
            raise ToolError('permission_denied', '不能操作密钥文件')
        if target.exists():
            raise ToolError('target_exists', '目标已存在；请使用新文件名')
        return target

    def create(self, value, content):
        target = self.write(value)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.write(value)
        with target.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return target
