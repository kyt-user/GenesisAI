"""CLI 模型提供商/密钥/模型的交互式配置与持久化验证。

覆盖：提供商目录、用户级配置读写与原子写入、按配置构建模型、
以及无 TTY 时的回退选择流程与 ``--auth`` 接线。
"""

from io import StringIO

import pytest
from rich.console import Console

from genesisai.app import onboarding
from genesisai.model import config as model_config
from genesisai.model.providers import catalog
from genesisai.model.settings import (
    ProviderSettings,
    load_settings,
    save_settings,
    settings_path,
)


def recording_console():
    stream = StringIO()
    return Console(file=stream, width=120, force_terminal=False), stream


def feed_inputs(monkeypatch, values):
    queue = iter(values)
    monkeypatch.setattr("rich.console.Console.input", lambda *args, **kwargs: next(queue))


def deepseek_settings(**overrides):
    base = dict(provider="deepseek", model="deepseek-chat", api_key="sk-test-key", base_url="https://api.deepseek.com")
    base.update(overrides)
    return ProviderSettings(**base)


def test_catalog_exposes_known_providers():
    assert catalog.provider_ids() == ["deepseek", "qwen"]
    deepseek = catalog.get_provider("deepseek")
    assert deepseek is not None
    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert deepseek.default_model in {model.id for model in deepseek.models}
    assert catalog.get_provider("") is None


def test_settings_path_honors_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom"
    monkeypatch.setenv("GENESISAI_CONFIG_DIR", str(target))
    assert settings_path() == target / "config.json"


def test_settings_roundtrip_masks_and_writes_atomically(tmp_path):
    directory = tmp_path / "cfg"
    settings = deepseek_settings(api_key="sk-abcdefghijkl")

    saved = save_settings(settings, directory)

    assert saved == settings_path(directory)
    assert not saved.with_suffix(".tmp").exists()
    assert load_settings(directory) == settings
    assert settings.masked_key() == "sk-a…ijkl"


def test_load_settings_returns_none_for_missing_corrupt_or_incomplete(tmp_path):
    directory = tmp_path / "cfg"
    assert load_settings(directory) is None

    directory.mkdir()
    target = directory / "config.json"
    target.write_text("{ not json", encoding="utf-8")
    assert load_settings(directory) is None

    target.write_text('{"provider": "deepseek"}', encoding="utf-8")
    assert load_settings(directory) is None


def test_build_model_from_settings_uses_selected_provider(tmp_path):
    client, remote = model_config.build_model(settings=deepseek_settings(model="deepseek-reasoner"))

    assert client.model == "deepseek-reasoner"
    assert remote is True


def test_build_model_from_settings_defaults_qwen_base_url():
    settings = ProviderSettings(provider="qwen", model="qwen-plus", api_key="sk-test-key")
    client, remote = model_config.build_model(settings=settings)

    assert client.model == "qwen-plus"
    assert remote is True
    assert "dashscope.aliyuncs.com" in str(client.client.base_url)


def test_build_model_rejects_unknown_settings_provider():
    with pytest.raises(ValueError):
        model_config.build_model(settings=ProviderSettings(provider="acme", model="x", api_key="sk"))


def test_mask_and_credentials_fall_back_to_environment(monkeypatch):
    assert onboarding.mask("") == "（未设置）"
    assert onboarding.mask("short") == "****"
    assert onboarding.mask("sk-abcdefghijkl") == "sk-a…ijkl"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    settings = ProviderSettings(provider="deepseek", model="deepseek-chat")
    assert onboarding.has_credentials(settings) is True
    assert onboarding.effective_key(settings) == "sk-env"
    assert onboarding.has_credentials(None) is False


def test_run_onboarding_persists_selection_without_tty(tmp_path, monkeypatch):
    console, stream = recording_console()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    feed_inputs(monkeypatch, ["1", "sk-chosen-key-123456", "1"])

    directory = tmp_path / "cfg"
    result = onboarding.run_onboarding(console, directory=directory)

    assert result is not None
    assert (result.provider, result.model) == ("deepseek", "deepseek-v4-flash")
    loaded = load_settings(directory)
    assert loaded is not None
    assert loaded.api_key == "sk-chosen-key-123456"
    assert "已保存" in stream.getvalue()


def test_run_onboarding_cancel_writes_nothing(tmp_path, monkeypatch):
    console, _ = recording_console()
    feed_inputs(monkeypatch, ["x"])

    directory = tmp_path / "cfg"
    assert onboarding.run_onboarding(console, directory=directory) is None
    assert not settings_path(directory).exists()


def test_select_model_only_switches_model_and_keeps_key(tmp_path, monkeypatch):
    console, _ = recording_console()
    directory = tmp_path / "cfg"
    save_settings(deepseek_settings(model="deepseek-v4-flash", api_key="sk-keep-key-123456"), directory)
    feed_inputs(monkeypatch, ["3"])

    updated = onboarding.select_model_only(console, load_settings(directory), directory=directory)

    assert updated is not None
    assert updated.model == "deepseek-reasoner"
    assert updated.api_key == "sk-keep-key-123456"
    assert load_settings(directory).model == "deepseek-reasoner"


def test_cli_auth_non_interactive_writes_config(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    stream = StringIO()
    monkeypatch.setattr(cli, "Console", lambda *args, **kwargs: Console(file=stream, width=120, force_terminal=False))
    monkeypatch.setattr(cli, "load_environment", lambda *args, **kwargs: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    feed_inputs(monkeypatch, ["1", "sk-cli-key-123456", "2"])

    directory = tmp_path / "cfg"
    work = tmp_path / "work"

    assert cli.main(["--auth", "--config-dir", str(directory), "--workspace", str(work)]) == 0

    saved = load_settings(directory)
    assert saved is not None
    assert (saved.provider, saved.model) == ("deepseek", "deepseek-chat")
    assert saved.api_key == "sk-cli-key-123456"
    assert "模型配置" in stream.getvalue()


def test_cli_missing_key_fails_fast_without_tty(tmp_path, monkeypatch):
    import genesisai.app.cli as cli

    stream = StringIO()
    monkeypatch.setattr(cli, "Console", lambda *args, **kwargs: Console(file=stream, width=120, force_terminal=False))
    monkeypatch.setattr(cli, "load_environment", lambda *args, **kwargs: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    directory = tmp_path / "cfg"
    save_settings(deepseek_settings(api_key=""), directory)
    work = tmp_path / "work"

    assert cli.main(["--config-dir", str(directory), "--workspace", str(work)]) == 2
    assert "API Key" in stream.getvalue()