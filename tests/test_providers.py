"""多 provider 注册表测试：内置 6 + 自定义合并 / 字段默认值 / key 解析 / client 构建。

用 monkeypatch 假 OpenAI 类，不真实联网。
"""

import json

import pytest

from phxsc import providers
from phxsc.providers import (
    DEFAULT_PROVIDER,
    ProviderKeyError,
    all_providers,
    build_client,
    get_provider,
    load_custom_providers,
    resolve_api_key,
)


class TestBuiltin:
    def test_builtin_has_six_providers(self):
        names = set(all_providers())
        assert {"deepseek", "zhipu", "openai", "anthropic", "kimi", "mimo"} <= names
        assert len(names) >= 6

    def test_builtin_deepseek_default(self):
        assert DEFAULT_PROVIDER == "deepseek"

    def test_builtin_openai_thinking_top_level(self):
        assert all_providers()["openai"]["thinking_style"] == "top_level"

    def test_builtin_deepseek_v4_flash_context_length(self):
        assert get_provider("deepseek")["models"]["deepseek-v4-flash"]["context_length"] == 1048576


class TestCustom:
    def test_custom_provider_appended(self, tmp_path):
        p = tmp_path / "providers.json"
        p.write_text(
            json.dumps(
                {"providers": {"myproxy": {"base_url": "https://proxy.example.com/v1"}}}
            ),
            encoding="utf-8",
        )
        merged = all_providers(str(p))
        assert "myproxy" in merged
        cfg = merged["myproxy"]
        assert cfg["base_url"] == "https://proxy.example.com/v1"
        assert cfg["api_key_env"] == "MYPROXY_API_KEY"
        assert cfg["auth"] == "bearer"
        assert cfg["thinking_style"] == "extra_body"
        assert cfg["status"] == "custom"

    def test_custom_overrides_builtin_skipped(self, tmp_path):
        p = tmp_path / "providers.json"
        p.write_text(
            json.dumps(
                {"providers": {"deepseek": {"base_url": "https://evil.example.com"}}}
            ),
            encoding="utf-8",
        )
        merged = all_providers(str(p))
        assert merged["deepseek"]["base_url"] == "https://api.deepseek.com"

    def test_custom_invalid_entry_skipped(self, tmp_path):
        p = tmp_path / "providers.json"
        p.write_text(
            json.dumps(
                {
                    "providers": {
                        "bad": {"base_url": "ftp://nope"},
                        "good": {"base_url": "https://ok.example.com/v1"},
                    }
                }
            ),
            encoding="utf-8",
        )
        merged = all_providers(str(p))
        assert "bad" not in merged
        assert "good" in merged

    def test_custom_missing_file_empty(self, tmp_path):
        assert load_custom_providers(str(tmp_path / "nope.json")) == {}


class TestResolveApiKey:
    def test_candidates_first_existing(self, monkeypatch):
        monkeypatch.setenv("A_KEY", "a-value")
        monkeypatch.setenv("B_KEY", "b-value")
        assert resolve_api_key({"api_key_env": "A_KEY,B_KEY"}) == "a-value"

    def test_candidates_skip_missing_take_second(self, monkeypatch):
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.setenv("B_KEY", "b-value")
        assert resolve_api_key({"api_key_env": "A_KEY,B_KEY"}) == "b-value"

    def test_candidates_all_missing_raises(self, monkeypatch):
        monkeypatch.delenv("A_KEY", raising=False)
        monkeypatch.delenv("B_KEY", raising=False)
        with pytest.raises(ProviderKeyError):
            resolve_api_key({"api_key_env": "A_KEY,B_KEY", "name": "x"})


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class TestBuildClient:
    def test_build_client_bearer(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
        monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
        client, name, model = build_client("deepseek")
        assert name == "deepseek"
        assert model == "deepseek-v4-flash"
        assert client.kwargs["api_key"] == "sk-deepseek"
        assert client.kwargs["base_url"] == "https://api.deepseek.com"

    def test_build_client_header_api_key(self, monkeypatch):
        monkeypatch.setenv("MIMO_API_KEY", "sk-mimo")
        monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
        client, name, model = build_client("mimo")
        assert name == "mimo"
        assert model == "mimo-v2.5-pro"
        assert client.kwargs["api_key"] == "sk-mimo"
        assert client.kwargs["default_headers"] == {"api-key": "sk-mimo"}

    def test_build_client_unknown_provider(self):
        with pytest.raises(ProviderKeyError) as exc_info:
            build_client("nonexistent-provider")
        assert "可用：deepseek" in str(exc_info.value)

    def test_build_client_model_default(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "sk-zhipu")
        monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
        _, name, model = build_client("zhipu", None)
        assert name == "zhipu"
        assert model == "glm-4.5-air"

    def test_build_client_model_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
        monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
        _, _, model = build_client("deepseek", "custom-model")
        assert model == "custom-model"

    def test_build_client_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ProviderKeyError) as exc_info:
            build_client("openai")
        assert "OPENAI_API_KEY" in str(exc_info.value)

    def test_get_provider_unknown_none(self, tmp_path):
        assert get_provider("does-not-exist", str(tmp_path / "nope.json")) is None
