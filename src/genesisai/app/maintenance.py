"""CLI 诊断与有边界的运行空间治理。"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path


def doctor(workspace, state_root, model_config=None):
    workspace, state_root = Path(workspace), Path(state_root)
    checks = []
    def add(name, ok, detail): checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
    add('workspace', workspace.is_dir() and os.access(workspace, os.R_OK), str(workspace))
    add('state_directory', state_root.is_dir() and os.access(state_root, os.W_OK), str(state_root))
    config = Path(model_config) if model_config else Path(__file__).resolve().parents[2] / 'config' / 'model.yaml'
    add('model_config', config.is_file(), str(config))
    add('api_key', bool(os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY')), '已配置' if (os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY')) else '缺少环境变量')
    for module in ('docx', 'openpyxl', 'pptx', 'pypdf', 'reportlab'):
        add('dependency:' + module, importlib.util.find_spec(module) is not None, 'available' if importlib.util.find_spec(module) else '安装 genesisai[office]')
    return {'ok': all(item['ok'] for item in checks[:4]), 'checks': checks}


def clean_candidates(state_root, current_session, *, days=30):
    root = Path(state_root).resolve()
    cutoff = time.time() - max(1, days) * 86400
    results = []
    for folder in ('command_logs', 'traces'):
        base = root / folder
        if not base.is_dir(): continue
        for path in base.glob('*'):
            if not path.is_file() or (folder == 'traces' and path.stem == current_session): continue
            if path.stat().st_mtime < cutoff or path.suffix == '.tmp':
                results.append({'path': str(path.resolve()), 'size': path.stat().st_size})
    return results


def clean_runtime(state_root, current_session, *, execute=False, days=30):
    root = Path(state_root).resolve()
    items = clean_candidates(root, current_session, days=days)
    if execute:
        for item in items:
            path = Path(item['path']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('清理目标越界')
            path.unlink(missing_ok=True)
    return {'execute': execute, 'count': len(items), 'bytes': sum(item['size'] for item in items), 'files': items}
