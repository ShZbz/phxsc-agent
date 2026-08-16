"""/provider 与 /model 跨 provider 增强测试（batch55 CLI 交互层）。

handler 直调模式：fake loop/client + monkeypatch cli.build_client / cli.all_providers /
cli.load_settings / cli.save_settings，不发真实网络请求、不落真实 settings.json。
"""

import time
from types import SimpleNamespace

import pytest

from phxsc.cli import (
    CONTEXT_WINDOW_TOKENS,
    _handle_model,
    _handle_provider,
    _render_toolbar,
    _UIState,
)
from phxsc.providers import ProviderKeyError


def make_loop(provider="deepseek", model="deepseek-v4-flash"):
    """最小 loop 假对象：provider/model 可变，stats() 含 provider。"""
    ctx = SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}])
    return SimpleNamespace(
        provider=provider,
        model=model,
        context=ctx,
        llm_client=None,
        last_usage={},
        stats=lambda: {
            "mode": "investigate",
            "provider": provider,
            "model": model,
            "steps": 1,
            "total_tokens": 10,
            "cache_hit": False,
            "last_usage": {},
            "prefix_hit_tokens": 0,
            "prefix_miss_tokens": 0,
            "prefix_hit_rate": 0.0,
        },
    )


class FakeClient:
    """记录 set_inner/set_provider 调用的最小 ThinkingLLM 替身。"""

    def __init__(self):
        self.set_inner_calls = []
        self.set_provider_calls = []

    def set_inner(self, inner) -> None:
        self.set_inner_calls.append(inner)

    def set_provider(self, provider: str) -> None:
        self.set_provider_calls.append(provider)


def raise_key_error(*args, **kwargs):
    raise ProviderKeyError("缺 key")


class TestProviderList:
    def test_no_arg_lists_all_with_marker(self, monkeypatch, capsys):
        loop = make_loop(provider="deepseek")
        monkeypatch.setattr(
            "phxsc.cli.all_providers",
            lambda: {
                "deepseek": {"status": "verified", "default_model": "deepseek-v4-flash"},
                "zhipu": {"status": "verified", "default_model": "glm-4.5-air"},
            },
        )
        _handle_provider(loop, FakeClient(), "/provider")
        out = capsys.readouterr().out
        assert "★ deepseek" in out
        assert "zhipu" in out
        assert "当前：deepseek/deepseek-v4-flash" in out


class TestProviderSwitch:
    def test_switch_success(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()
        fake_raw = object()
        monkeypatch.setattr(
            "phxsc.cli.build_client",
            lambda p, m: (fake_raw, "zhipu", "glm-4.5-air"),
        )
        monkeypatch.setattr("phxsc.cli.load_settings", lambda: {})
        saved = {}
        monkeypatch.setattr("phxsc.cli.save_settings", lambda s: saved.update(s))
        _handle_provider(loop, client, "/provider zhipu")
        assert client.set_inner_calls == [fake_raw]
        assert client.set_provider_calls == ["zhipu"]
        assert loop.provider == "zhipu"
        assert loop.model == "glm-4.5-air"
        assert saved == {"provider": "zhipu", "model": "glm-4.5-air"}
        assert "provider 已切换：zhipu/glm-4.5-air" in capsys.readouterr().out

    def test_switch_key_error_keeps_state(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()
        monkeypatch.setattr("phxsc.cli.build_client", raise_key_error)
        _handle_provider(loop, client, "/provider openai")
        assert "错误：" in capsys.readouterr().out
        assert loop.provider == "deepseek"
        assert loop.model == "deepseek-v4-flash"
        assert client.set_inner_calls == []

    def test_switch_unknown_keeps_state(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()

        def unknown(p, m):
            raise ProviderKeyError("未知 provider：bogus")

        monkeypatch.setattr("phxsc.cli.build_client", unknown)
        _handle_provider(loop, client, "/provider bogus")
        assert "错误：" in capsys.readouterr().out
        assert loop.provider == "deepseek"
        assert client.set_inner_calls == []

    def test_usage_on_extra_args(self, capsys):
        loop = make_loop()
        _handle_provider(loop, FakeClient(), "/provider a b")
        assert "用法：/provider [名称]" in capsys.readouterr().out
        assert loop.provider == "deepseek"


class TestModelCommand:
    def test_no_arg_shows_provider_model(self, capsys):
        loop = make_loop(provider="zhipu", model="glm-4.5-air")
        _handle_model(loop, FakeClient(), "/model")
        assert "zhipu/glm-4.5-air" in capsys.readouterr().out

    def test_switch_within_provider(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()
        monkeypatch.setattr("phxsc.cli.load_settings", lambda: {})
        saved = {}
        monkeypatch.setattr("phxsc.cli.save_settings", lambda s: saved.update(s))
        _handle_model(loop, client, "/model xyz")
        assert loop.model == "xyz"
        assert loop.provider == "deepseek"
        assert saved == {"model": "xyz"}
        assert client.set_inner_calls == []
        assert "deepseek/xyz" in capsys.readouterr().out

    def test_switch_cross_provider(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()
        fake_raw = object()
        monkeypatch.setattr(
            "phxsc.cli.build_client",
            lambda p, m: (fake_raw, "zhipu", "glm-4.5-air"),
        )
        monkeypatch.setattr("phxsc.cli.load_settings", lambda: {})
        saved = {}
        monkeypatch.setattr("phxsc.cli.save_settings", lambda s: saved.update(s))
        _handle_model(loop, client, "/model zhipu/glm-4.5-air")
        assert loop.provider == "zhipu"
        assert loop.model == "glm-4.5-air"
        assert client.set_inner_calls == [fake_raw]
        assert client.set_provider_calls == ["zhipu"]
        assert saved == {"provider": "zhipu", "model": "glm-4.5-air"}
        assert "模型已切换：zhipu/glm-4.5-air" in capsys.readouterr().out

    def test_switch_cross_provider_key_error_keeps_state(self, monkeypatch, capsys):
        loop = make_loop()
        client = FakeClient()
        monkeypatch.setattr("phxsc.cli.build_client", raise_key_error)
        _handle_model(loop, client, "/model openai/gpt-4o")
        assert "错误：" in capsys.readouterr().out
        assert loop.provider == "deepseek"
        assert loop.model == "deepseek-v4-flash"
        assert client.set_inner_calls == []


class TestToolbar:
    def test_shows_provider_model(self):
        loop = make_loop(provider="deepseek", model="deepseek-v4-flash")
        text = _render_toolbar(_UIState(loop, time.perf_counter()))
        assert "deepseek/deepseek-v4-flash" in text

    def test_ctx_estimate_prefixed_when_no_usage(self):
        """U5：无真实 usage → ctx 段用估算值且带 ~ 前缀。"""
        loop = make_loop()
        text = _render_toolbar(_UIState(loop, time.perf_counter()))
        assert "~" in text
        assert f"/{CONTEXT_WINDOW_TOKENS}" in text

    def test_ctx_real_prompt_tokens_when_usage(self):
        """U5：last_usage 有 prompt_tokens → ctx 段用真实值且无 ~ 前缀。"""
        loop = make_loop()
        loop.last_usage = {"prompt_tokens": 123}
        text = _render_toolbar(_UIState(loop, time.perf_counter()))
        assert "123/" in text
        assert "~" not in text


class TestMainProviderModelFlags:
    """batch2#10：--provider/--model 显式传默认值不再被 settings 覆盖。

    main() 级测试：settings.json（tmp）为 zhipu 时，显式 --provider deepseek 生效；
    未传时仍恢复 settings。mock build_client 记录调用，--no-tui + 非 tty 避免真实网络/tty。
    """

    def _run_main(self, monkeypatch, tmp_path, argv, provider, model):
        """mock main() 全套依赖；settings.json 指向 tmp；返回 (exit_code, build_client 调用)。"""
        import phxsc.cli as cli

        calls: list[tuple] = []

        def fake_build_client(p, m):
            calls.append((p, m))
            return (object(), p or "deepseek", m or "deepseek-v4-flash")

        monkeypatch.setattr(cli, "build_client", fake_build_client)
        monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
        monkeypatch.setattr(cli, "_resolve_workdir", lambda w: str(tmp_path / "ws"))
        monkeypatch.setattr(
            cli, "Telemetry",
            lambda: SimpleNamespace(daily_summary=lambda: {"calls": 0}, close=lambda: None),
        )
        monkeypatch.setattr(cli, "EmbedCache", lambda p: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "SemanticCache", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "ExactCache", lambda p: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(cli, "scan_skills", lambda: [])
        monkeypatch.setattr(cli, "build_metadata_table", lambda metas: "")
        monkeypatch.setattr(cli, "load_config", lambda: {"servers": []})
        monkeypatch.setattr(cli, "MemoryStore", lambda p: object())
        monkeypatch.setattr(cli, "create_gate", lambda c, s, model=None: None)
        monkeypatch.setattr(
            cli, "SessionStore",
            lambda p: SimpleNamespace(create_session=lambda m: "s1", close=lambda: None),
        )
        monkeypatch.setattr(
            cli, "create_scheduler",
            lambda a, b: SimpleNamespace(start=lambda: None, stop=lambda: None),
        )
        fake_loop = SimpleNamespace(
            mode="investigate", provider="deepseek", model="deepseek-v4-flash",
            voice="academic",
            llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
            context=SimpleNamespace(build_messages=lambda: [{"role": "system", "content": "sys"}]),
            semantic_hit=None, cache_hit=False,
            stats=lambda: {
                "mode": "investigate", "provider": "deepseek", "model": "deepseek-v4-flash",
            },
        )
        monkeypatch.setattr(cli, "_build_loop", lambda *a, **k: fake_loop)
        monkeypatch.setattr(cli, "_input_line", lambda session, prompt: "/exit")
        (tmp_path / "settings.json").write_text(
            f'{{"provider": "{provider}", "model": "{model}"}}',
            encoding="utf-8",
        )
        monkeypatch.setattr("phxsc.settings.DEFAULT_PATH", str(tmp_path / "settings.json"))
        result = cli.main(argv)
        return result, calls

    def test_explicit_provider_default_value_wins_over_settings(self, monkeypatch, tmp_path):
        result, calls = self._run_main(
            monkeypatch, tmp_path,
            ["--no-tui", "--provider", "deepseek"],
            provider="zhipu", model="glm-4.5-air",
        )
        assert result == 0
        assert calls == [("deepseek", None)]

    def test_explicit_model_default_value_wins_over_settings(self, monkeypatch, tmp_path):
        result, calls = self._run_main(
            monkeypatch, tmp_path,
            ["--no-tui", "--model", "deepseek-v4-flash"],
            provider="zhipu", model="glm-4.5-air",
        )
        assert result == 0
        assert calls == [("zhipu", "deepseek-v4-flash")]

    def test_explicit_both_default_values_win_over_settings(self, monkeypatch, tmp_path):
        result, calls = self._run_main(
            monkeypatch, tmp_path,
            ["--no-tui", "--provider", "deepseek", "--model", "deepseek-v4-flash"],
            provider="zhipu", model="glm-4.5-air",
        )
        assert result == 0
        assert calls == [("deepseek", "deepseek-v4-flash")]

    def test_no_flags_restores_settings(self, monkeypatch, tmp_path):
        result, calls = self._run_main(
            monkeypatch, tmp_path,
            ["--no-tui"],
            provider="zhipu", model="glm-4.5-air",
        )
        assert result == 0
        assert calls == [("zhipu", "glm-4.5-air")]
