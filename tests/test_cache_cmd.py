"""/cache 斜杠命令测试（batch28）。

覆盖：/cache stats 三表输出、/cache clear semantic|exact|all 确认式清空、
取消（n）、非法参数用法提示。全部用 tmp_path 真 SQLite + fake input，不碰真实 API。
"""

import numpy as np
import pytest
from rich.console import Console

from phxsc.cache.embed_cache import EmbedCache
from phxsc.cache.exact import ExactCache
from phxsc.cache.semantic import SemanticCache
from phxsc.cli import _handle_cache


def _vec(dim=16, seed=1):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def caches(tmp_path):
    exact = ExactCache(str(tmp_path / "exact.db"))
    semantic = SemanticCache(str(tmp_path / "semantic.db"))
    embed = EmbedCache(str(tmp_path / "embed.db"))
    yield exact, semantic, embed
    exact.close()
    semantic.close()
    embed.close()


def _embed_count(embed):
    return embed._conn.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]


class TestCacheStats:
    def test_stats_shows_exact_semantic_embed_rows(self, caches):
        exact, semantic, embed = caches
        exact.set("k", "v")
        semantic.store("q", "a", "investigate", "academic", _vec())
        embed.set("q", _vec())
        console = Console(record=True)
        _handle_cache(None, console, exact, semantic, embed, "/cache stats")
        text = console.export_text()
        assert "exact" in text
        assert "semantic" in text
        assert "embed" in text
        assert "缓存" in text
        assert text.count("1") >= 3  # 三表 entries 各 1

    def test_stats_zero_state(self, caches):
        exact, semantic, embed = caches
        console = Console(record=True)
        _handle_cache(None, console, exact, semantic, embed, "/cache stats")
        text = console.export_text()
        assert "exact" in text and "semantic" in text and "embed" in text


class TestCacheClear:
    def test_clear_semantic_confirmed(self, caches, monkeypatch):
        exact, semantic, embed = caches
        exact.set("k", "v")
        semantic.store("q", "a", "investigate", "academic", _vec())
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        _handle_cache(None, Console(), exact, semantic, embed, "/cache clear semantic")
        assert exact.stats()["entries"] == 1
        assert semantic.stats()["entries"] == 0

    def test_clear_exact_confirmed(self, caches, monkeypatch):
        exact, semantic, embed = caches
        exact.set("k", "v")
        semantic.store("q", "a", "investigate", "academic", _vec())
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        _handle_cache(None, Console(), exact, semantic, embed, "/cache clear exact")
        assert exact.stats()["entries"] == 0
        assert semantic.stats()["entries"] == 1

    def test_clear_exact_then_get_returns_none(self, caches, monkeypatch):
        exact, semantic, embed = caches
        key = exact.key_for("q", "m")
        exact.set(key, "v")
        embed.set("e", _vec())
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        _handle_cache(None, Console(), exact, semantic, embed, "/cache clear all")
        assert exact.get(key) is None
        assert embed.get("e") is None

    def test_clear_all_empties_everything(self, caches, monkeypatch):
        exact, semantic, embed = caches
        exact.set("k", "v")
        semantic.store("q", "a", "investigate", "academic", _vec())
        embed.set("q", _vec())
        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        _handle_cache(None, Console(), exact, semantic, embed, "/cache clear all")
        assert exact.stats()["entries"] == 0
        assert semantic.stats()["entries"] == 0
        assert _embed_count(embed) == 0

    def test_clear_cancelled_with_n_keeps_data(self, caches, monkeypatch):
        exact, semantic, embed = caches
        exact.set("k", "v")
        semantic.store("q", "a", "investigate", "academic", _vec())
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        console = Console(record=True)
        _handle_cache(console=console, session=None, exact_cache=exact, semantic_cache=semantic, embed_cache=embed, line="/cache clear all")
        assert exact.stats()["entries"] == 1
        assert semantic.stats()["entries"] == 1
        assert "取消" in console.export_text()

    def test_clear_invalid_target_shows_usage(self, caches):
        exact, semantic, embed = caches
        console = Console(record=True)
        _handle_cache(None, console, exact, semantic, embed, "/cache clear bogus")
        text = console.export_text()
        assert "用法" in text
        assert "semantic|exact|all" in text
        assert exact.stats()["entries"] == 0

    def test_no_subcommand_shows_usage(self, caches):
        exact, semantic, embed = caches
        console = Console(record=True)
        _handle_cache(None, console, exact, semantic, embed, "/cache")
        assert "用法" in console.export_text()
