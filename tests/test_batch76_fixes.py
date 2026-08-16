"""batch76 第二批修复测试：状态栏 cache% 当前会话口径 + STATUS 页当日口径标注。

用户复测 bug（user_log_2 #2）：启动时 ctx 0% 但 cache 显示历史累计 25%——
状态栏 cache% 此前读 telemetry 当日累计（含历史会话调用），与 ctx% 当前
会话实时口径混显示。修复（写死口径）：
- 状态栏 cache% 改读 loop 实例 prefix_hit/miss_tokens（本会话累计），
  无调用数据 → 整段隐藏（prefix_rate None 既有机制）
- /new 归零 loop prefix 字段（_handle_new），状态栏随之隐藏
- STATUS 页保留 telemetry 当日累计口径但显式标注 "(当日累计)"

fake loop / 假 telemetry，不发真实网络请求、不拉起真实 CLI。
"""

import threading
from types import SimpleNamespace

from rich.console import Console

from phxsc.cli import _handle_new
from phxsc.ui.app import PhyScApp
from phxsc.ui.events import EventBus
from phxsc.ui.screens.status import build_status_text
from phxsc.ui.state import UIState
from phxsc.ui.widgets.status_bar import loop_prefix_rate



def make_session_loop(hit=0, miss=0):
    """带 prefix token 字段 + context.reset 的假 loop（状态栏 /new 均读它）。"""
    return SimpleNamespace(
        mode="investigate",
        provider="deepseek",
        model="deepseek-v4-flash",
        voice="academic",
        llm_client=SimpleNamespace(level=SimpleNamespace(value="high")),
        interrupt_event=threading.Event(),
        context=SimpleNamespace(reset=lambda: None, build_messages=lambda: []),
        prefix_hit_tokens=hit,
        prefix_miss_tokens=miss,
        run=lambda text, gate_round=False: f"回答：{text}",
    )


def make_session_app(hit=0, miss=0):
    return PhyScApp(bus=EventBus(), loop=make_session_loop(hit, miss))


# ---- F1：loop_prefix_rate 纯函数（状态栏 cache% 数据源）----


class TestLoopPrefixRate:
    def test_no_calls_returns_none(self):
        assert loop_prefix_rate(make_session_loop(0, 0)) is None

    def test_ratio_from_session_tokens(self):
        assert loop_prefix_rate(make_session_loop(750, 250)) == 0.75
        assert loop_prefix_rate(make_session_loop(800, 200)) == 0.8

    def test_all_hit_is_one(self):
        assert loop_prefix_rate(make_session_loop(1000, 0)) == 1.0

    def test_missing_fields_returns_none(self):
        assert loop_prefix_rate(SimpleNamespace()) is None

    def test_none_loop_returns_none(self):
        assert loop_prefix_rate(None) is None


# ---- F2：状态栏 cache% 当前会话口径 ----

class TestStatusBarSessionCache:
    def test_shows_session_percent_when_calls_exist(self, run_test):
        app = make_session_app(750, 250)

        async def drive(app, pilot):
            app.status_bar.refresh_status()
            label = app.query_one("#status-label").render().plain
            assert "cache 75%" in label

        run_test(app, drive=drive)

    def test_hides_segment_without_calls(self, run_test):
        app = make_session_app(0, 0)

        async def drive(app, pilot):
            app.status_bar.refresh_status()
            label = app.query_one("#status-label").render().plain
            assert "cache" not in label
            assert "ctx" in label  # ctx 段不受影响

        run_test(app, drive=drive)


# ---- F3：/new 归零 loop prefix 字段 → 状态栏 cache 隐藏 ----


class TestNewResetsPrefix:
    def test_handle_new_zeroes_prefix_fields(self):
        loop = make_session_loop(750, 250)
        console = Console(record=True)
        _handle_new(loop, console)
        assert loop.prefix_hit_tokens == 0
        assert loop.prefix_miss_tokens == 0
        assert "已开启新会话" in console.export_text()

    def test_new_hides_cache_in_status_bar(self, run_test):
        app = make_session_app(750, 250)

        async def drive(app, pilot):
            app.status_bar.refresh_status()
            assert "cache 75%" in app.query_one("#status-label").render().plain
            app.dispatch_command("/new")
            await pilot.pause()
            assert app.loop.prefix_hit_tokens == 0
            assert app.loop.prefix_miss_tokens == 0
            label = app.query_one("#status-label").render().plain
            assert "cache" not in label

        run_test(app, drive=drive)


# ---- F4：STATUS 页当日累计口径标注 ----


class TestStatusPageDailyAnnotation:
    def test_prefix_line_annotated(self, tmp_path):
        services = SimpleNamespace(
            telemetry=SimpleNamespace(
                daily_summary=lambda: {
                    "prefix_cache_hit_rate": 0.25,
                    "cache_hit_rate": 0.1,
                    "semantic_hit_rate": 0.2,
                    "estimated_cost_usd": 0.01,
                }
            ),
            loaded_skills={},
            mcp_registry=None,
            scheduler=None,
        )
        text = str(
            build_status_text(
                make_session_loop(), UIState(), services, str(tmp_path / "nope")
            )
        )
        assert "prefix" in text
        assert "25% (当日累计)" in text

    def test_no_services_still_renders(self, tmp_path):
        text = str(
            build_status_text(
                make_session_loop(), UIState(), None, str(tmp_path / "nope")
            )
        )
        assert "CACHE" in text
