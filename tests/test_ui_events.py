"""UI 事件层测试（batch56）：EventBus 线程安全 / UIState 状态机 / theme token / keymap。

覆盖 UI_DESIGN §2 事件契约 + §4 色板 + §8 快捷键；纯逻辑单测，不拉起 Textual。
"""

import threading

import pytest

from phxsc.ui import events as ev
from phxsc.ui.keymap import KEYMAP, describe
from phxsc.ui.state import UIState
from phxsc.ui.theme import MODE_ACCENT, TOKENS, mode_accent


class TestEventBus:
    def test_bus_subscribe_publish(self):
        bus = ev.EventBus()
        got = []
        bus.subscribe(ev.EVENT_TOOL_STARTED, lambda kind, payload: got.append((kind, payload)))
        bus.publish(ev.EVENT_TOOL_STARTED, name="arxiv_search", args="perovskite")
        assert got == [(ev.EVENT_TOOL_STARTED, {"name": "arxiv_search", "args": "perovskite"})]

    def test_bus_unsubscribed_kind_not_delivered(self):
        bus = ev.EventBus()
        got = []
        bus.subscribe(ev.EVENT_TOOL_STARTED, lambda kind, payload: got.append(kind))
        bus.publish(ev.EVENT_TOOL_SUCCEEDED, name="x", duration=0.5, summary="ok")
        assert got == []

    def test_bus_thread_safe(self):
        bus = ev.EventBus()
        counter = {"n": 0}
        lock = threading.Lock()

        def on_event(kind, payload):
            with lock:
                counter["n"] += 1

        bus.subscribe(ev.EVENT_CACHE_HIT, on_event)
        threads = []
        for _ in range(10):
            t = threading.Thread(target=lambda: [bus.publish(ev.EVENT_CACHE_HIT, payload={"kind": "exact", "score": 0.9}) for _ in range(100)])
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert counter["n"] == 1000

    def test_bus_handler_error_isolated(self):
        bus = ev.EventBus()
        good = []

        def bad(kind, payload):
            raise RuntimeError("boom")

        def good_handler(kind, payload):
            good.append(kind)

        bus.subscribe(ev.EVENT_ERROR, bad)
        bus.subscribe(ev.EVENT_ERROR, good_handler)
        bus.publish(ev.EVENT_ERROR, message="x")  # 不抛异常
        assert good == [ev.EVENT_ERROR]


class TestStreamingEventConstants:
    def test_streaming_event_constants_defined(self):
        assert ev.EVENT_THINKING_CHUNK == "thinking_chunk"
        assert ev.EVENT_AGENT_CHUNK == "agent_chunk"

    def test_streaming_chunks_flow_via_key_value_publish(self):
        """两个增量事件载荷无 kind 键 → 键值形式 publish 携带 text。"""
        bus = ev.EventBus()
        got = []
        bus.subscribe(ev.EVENT_THINKING_CHUNK, lambda kind, payload: got.append((kind, payload)))
        bus.subscribe(ev.EVENT_AGENT_CHUNK, lambda kind, payload: got.append((kind, payload)))
        bus.publish(ev.EVENT_THINKING_CHUNK, text="推理")
        bus.publish(ev.EVENT_AGENT_CHUNK, text="回答")
        assert got == [
            (ev.EVENT_THINKING_CHUNK, {"text": "推理"}),
            (ev.EVENT_AGENT_CHUNK, {"text": "回答"}),
        ]


class TestUIState:
    def test_state_transitions(self):
        s = UIState()
        s.handle(ev.EVENT_AGENT_STARTED, {})
        assert s.running is True
        s.handle(ev.EVENT_TOOL_STARTED, {"name": "arxiv_search", "args": "q"})
        assert s.running is True
        assert s.phase == "searching"
        assert s.current_tool == "arxiv_search"
        s.handle(ev.EVENT_TOOL_SUCCEEDED, {"name": "arxiv_search", "duration": 0.84, "summary": "12 results"})
        assert s.running is True  # 工具完成不结束整轮（多步 loop 仍在 worker 线程）
        assert s.current_tool is None
        assert s.tool_history[-1] == {
            "name": "arxiv_search", "status": "success", "duration": 0.84, "summary": "12 results",
        }
        s.handle(ev.EVENT_AGENT_COMPLETED, {"duration": 3.2, "artifacts": []})
        assert s.running is False
        assert s.phase == "done"
        assert s.elapsed == 3.2

    def test_state_failed(self):
        s = UIState()
        s.handle(ev.EVENT_AGENT_STARTED, {})
        s.handle(ev.EVENT_TOOL_STARTED, {"name": "pdf_parse", "args": "f.pdf"})
        assert s.phase == "reading"
        s.handle(ev.EVENT_TOOL_FAILED, {"name": "pdf_parse", "error": "E", "reason": "parse error", "fix_hint": "reinstall"})
        assert s.last_error == "parse error"
        assert s.phase == "error"
        assert s.running is True  # 工具失败不结束整轮（loop 可能继续）
        assert s.tool_history[-1]["status"] == "failed"
        assert s.tool_history[-1]["reason"] == "parse error"
        assert s.tool_history[-1]["fix_hint"] == "reinstall"

    def test_running_kept_through_multi_step_round(self):
        """batch93 P1：多步工具轮窗口期内 running 保持 True，防并发第二 worker。"""
        s = UIState()
        s.handle(ev.EVENT_AGENT_STARTED, {})
        for i in range(3):
            s.handle(ev.EVENT_TOOL_STARTED, {"name": f"t{i}", "args": "q"})
            s.handle(ev.EVENT_TOOL_SUCCEEDED, {"name": f"t{i}", "duration": 0.1, "summary": "ok"})
        assert s.running is True
        s.handle(ev.EVENT_AGENT_COMPLETED, {"duration": 1.0, "artifacts": []})
        assert s.running is False

    def test_state_cache_stats(self):
        s = UIState()
        s.handle(ev.EVENT_CACHE_HIT, {"kind": "exact", "score": 0.99})
        s.handle(ev.EVENT_CACHE_HIT, {"kind": "semantic", "score": 0.95})
        assert s.cache_hits == 2
        assert s.last_cache == {"kind": "semantic", "score": 0.95}
        s.handle(ev.EVENT_CACHE_MISS, {"kind": "prefix"})
        assert s.cache_misses == 1
        assert s.cache_percent == 67
        assert s.last_cache is None

    def test_state_mode_model(self):
        s = UIState()
        s.handle(ev.EVENT_MODE_CHANGED, {"mode": "plan"})
        s.handle(ev.EVENT_SESSION_CHANGED, {"session_id": "abc", "title": "perovskite"})
        s.handle(ev.EVENT_MODEL_CHANGED, {"provider": "zhipu", "model": "glm-4.5-air"})
        s.handle(ev.EVENT_VOICE_CHANGED, {"voice": "natural"})
        s.handle(ev.EVENT_THINKING_CHANGED, {"level": "low"})
        assert s.mode == "plan"
        assert s.session_id == "abc"
        assert s.session_title == "perovskite"
        assert s.provider == "zhipu"
        assert s.model == "glm-4.5-air"
        assert s.voice == "natural"
        assert s.thinking_level == "low"

    def test_state_gate_reset(self):
        s = UIState()
        s.handle(ev.EVENT_GATE_STARTED, {"question": "q"})
        assert s.gate is True
        s.handle(ev.EVENT_AGENT_COMPLETED, {"duration": 1.0, "artifacts": []})
        assert s.gate is False

    def test_state_task_phase(self):
        s = UIState()
        s.handle(ev.EVENT_TASK_PHASE_CHANGED, {"phase": "reading", "step": 2, "total": 5, "label": "Reading paper 3/7"})
        assert s.phase == "reading"
        assert s.task_step == 2
        assert s.task_total == 5
        assert s.task_label == "Reading paper 3/7"

    def test_state_unknown_event_ignored(self):
        s = UIState()
        before = (s.phase, s.running, s.current_tool)
        s.handle("no_such_event", {"anything": 1})
        assert (s.phase, s.running, s.current_tool) == before

    def test_context_percent_bounds(self):
        s = UIState()
        assert s.context_percent == 0  # total=0 不除零
        s.context_used = 99999
        s.context_total = 1000
        assert s.context_percent == 100  # 超上限钳制
        s.context_used = 500
        s.context_total = 1000
        assert s.context_percent == 50

    def test_status_line_ctx_length_and_cache(self):
        s = UIState()
        s.context_used = 78000
        s.context_total = 131072
        s.prefix_rate = 0.875
        s.cost = 0.12345
        line = s.status_line()
        assert "78k/131k 60%" in line
        assert "cache 88%" in line
        assert "$0.12345" in line

    def test_status_line_hides_cache_without_data(self):
        s = UIState()
        s.prefix_rate = None
        line = s.status_line()
        assert "cache" not in line

    def test_status_line_cost_none_shows_unpriced(self):
        s = UIState()
        s.cost = None
        line = s.status_line()
        assert "成本 未定价" in line
        assert "$" not in line


class TestTheme:
    def test_theme_tokens(self):
        assert set(TOKENS) == {
            "bg", "text1", "text2", "text3", "border",
            "mode_plan", "mode_investigate", "mode_typeset",
            "success", "warning", "error", "logo_grad",
        }
        assert set(TOKENS["logo_grad"]) == {"plan", "investigate", "typeset"}
        for k in ("mode_plan", "mode_investigate", "mode_typeset"):
            assert TOKENS[k].startswith("#")
        assert mode_accent("plan") == TOKENS["mode_plan"]
        assert mode_accent("investigate") == TOKENS["mode_investigate"]
        assert mode_accent("typeset") == TOKENS["mode_typeset"]
        assert mode_accent("bogus") == TOKENS["text2"]
        assert set(MODE_ACCENT) == {"plan", "investigate", "typeset"}


class TestKeymap:
    def test_keymap_has_core_keys(self):
        for key in ("tab", "ctrl+p", "ctrl+c", "escape", "?"):
            assert key in KEYMAP
        assert KEYMAP["tab"][0] == "switch_mode"
        assert KEYMAP["ctrl+c"][0] == "interrupt"

    def test_keymap_describe(self):
        assert describe("tab") == "切换模式"
        assert describe("nope") == ""
