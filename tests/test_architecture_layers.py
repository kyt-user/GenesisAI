"""架构防线：五层单向依赖与 shared 独立性由 import-linter 机械强制。"""

from __future__ import annotations

from pathlib import Path

from importlinter import configuration as importlinter_configuration
from importlinter.application.use_cases import lint_imports

# 直接调用 use_cases 前必须先注册用户选项读取器（CLI 入口会自动完成）。
importlinter_configuration.configure()

ROOT = Path(__file__).resolve().parents[1]


def test_layer_and_independence_contracts_pass():
    """S3 退出条件：五层契约通过（app←core←agent←model←shared + shared 独立）。"""
    assert lint_imports(
        config_filename=str(ROOT / "pyproject.toml"),
        cache_dir=None,
        no_logo=True,
    ) is True