"""PhySc-agent Textual 主应用（batch57 骨架 + batch58 过程可视化挂载）。

布局（UI_DESIGN §3.1）：垂直 = Header(1) + Main(fr) + Composer(auto) + StatusBar(1)；
Main 内横向 = TabbedContent(CHAT/ACTIVITY) + Inspector（≥120 列显示）。
CHAT = 消息/工具卡片流；Inspector 下半区挂 TaskProgress（batch65 F10 移位）；
ACTIVITY = 研究过程时间线（batch58）；STATUS = 全量状态页（batch60）。
交互层 overlays（batch60）：Command Palette / Session picker / Help / Model picker
用 push_screen(ModalScreen) 弹出。

事件接线（batch56 契约）：App 在 on_mount 订阅 bus 事件，回调统一经 _ui_call
（跨线程切回 UI 线程）更新 UIState + 各组件；mode_changed 驱动 Header /
ModeSelector / Composer badge 刷新；tool/cache/context 事件驱动 ChatView /
ActivityView / Inspector / StatusBar 实时刷新。
模式切换复用 cli.py 的既有路径（loop.mode 赋值，上下文保留）——UI 只做触发与
展示，框架主权在总控。
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from pathlib import Path

from rich.console import Console
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static, TabbedContent, TabPane, Tabs

from phxsc.agent.modes import MODE_NAMES
from phxsc.cli import (
    _gate_question,
    _handle_cache,
    _handle_dedup,
    _handle_mcp,
    _handle_moa,
    _handle_new,
    _handle_schedule,
    _handle_skill,
    _handle_thinking,
    _handle_voice,
    _parse_dedup,
)
from phxsc.ui.events import (
    EVENT_AGENT_CHUNK,
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_INTERRUPTED,
    EVENT_AGENT_MESSAGE,
    EVENT_AGENT_STARTED,
    EVENT_ARTIFACT_CREATED,
    EVENT_CACHE_HIT,
    EVENT_CACHE_MISS,
    EVENT_CONTEXT_USAGE,
    EVENT_ERROR,
    EVENT_EVIDENCE_FOUND,
    EVENT_PAPER_FOUND,
    EVENT_THINKING_CHUNK,
    EVENT_THINKING_STARTED,
    EVENT_THINKING_ENDED,
    EVENT_GATE_STARTED,
    EVENT_MODE_CHANGED,
    EVENT_MODEL_CHANGED,
    EVENT_SESSION_CHANGED,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_THINKING_CHANGED,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EVENT_VOICE_CHANGED,
    EventBus,
)
from phxsc.ui.screens.activity import ActivityView
from phxsc.ui.screens.chat import ChatView
from phxsc.ui.screens.status import StatusView
from phxsc.ui.state import UIState
from phxsc.ui.theme import TOKENS
from phxsc.ui.overlays.command_palette import CommandPalette
from phxsc.ui.overlays.help import HelpModal
from phxsc.ui.overlays.model_picker import ModelPicker
from phxsc.ui.overlays.session_picker import SessionPicker
from phxsc.ui.widgets.composer import Composer
from phxsc.ui.widgets.header import PhyScHeader
from phxsc.ui.widgets.inspector import Inspector
from phxsc.ui.widgets.mode_selector import ModeSelector
from phxsc.ui.widgets.status_bar import StatusBar

# 五档断点（UI_DESIGN §3.2）：<80 极简 / 80-99 完整状态栏无 Inspector /
# 100-119 Inspector 窄版(28) / 120-139 完整四区(默认) / ≥140 Inspector 加宽(36)
_BP_MINIMAL = 80
_BP_COMPACT = 100
_BP_FULL = 120
_BP_WIDE = 140
_INSPECTOR_NARROW = 28
_INSPECTOR_WIDE = 36

# 订阅事件集：mode 变化驱动 Header/ModeSelector/Composer badge；
# 运行态/工具/cache 变化驱动 StatusBar / ChatView / ActivityView / Inspector。
_SUBSCRIBED_KINDS = (
    EVENT_MODE_CHANGED,
    EVENT_AGENT_STARTED,
    EVENT_AGENT_COMPLETED,
    EVENT_AGENT_INTERRUPTED,
    EVENT_AGENT_MESSAGE,
    EVENT_AGENT_CHUNK,
    EVENT_CONTEXT_USAGE,
    EVENT_ERROR,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EVENT_TOOL_FAILED,
    EVENT_CACHE_HIT,
    EVENT_CACHE_MISS,
    EVENT_EVIDENCE_FOUND,
    EVENT_PAPER_FOUND,
    EVENT_THINKING_STARTED,
    EVENT_THINKING_ENDED,
    EVENT_THINKING_CHUNK,
    EVENT_ARTIFACT_CREATED,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_GATE_STARTED,
    EVENT_SESSION_CHANGED,
    EVENT_MODEL_CHANGED,
    EVENT_VOICE_CHANGED,
    EVENT_THINKING_CHANGED,
)


class PhyScApp(App[None]):
    """Textual 主应用：Header + Main(Chat/Inspector) + Composer + StatusBar。"""

    TITLE = "PhySc-agent"
    # batch D 自实现命令面板，禁用 Textual 默认 ctrl+p
    COMMAND_PALETTE_BINDING = None

    CSS = f"""
    Screen {{
        background: {TOKENS["bg"]};
    }}
    #shell {{
        height: 1fr;
    }}
    #main {{
        height: 1fr;
    }}
    .placeholder {{
        color: {TOKENS["text3"]};
        padding: 1 2;
    }}
    .msg-system {{
        color: {TOKENS["text2"]};
    }}
    TabbedContent {{
        width: 1fr;
        height: 1fr;
    }}
    #inspector {{
        width: 28;
        border-left: solid {TOKENS["border"]};
        color: {TOKENS["text2"]};
        padding: 1 1;
    }}
    Markdown {{
        background: {TOKENS["bg"]};
        color: {TOKENS["text1"]};
    }}
    """

    BINDINGS = [
        Binding("tab", "switch_mode", "切换模式", priority=True),
        Binding("shift+tab", "switch_mode_prev", "切换模式(反向)", priority=True),
        Binding("ctrl+p", "command_palette", "命令面板"),
        Binding("ctrl+l", "session_list", "会话列表"),
        Binding("ctrl+m", "model_picker", "模型选择"),
        Binding("ctrl+c", "interrupt", "中断当前任务", priority=True),
        Binding("ctrl+j", "scroll_bottom", "回对话底部"),
        Binding("escape", "close_overlay", "关闭浮层"),
        Binding("?", "help", "帮助"),
    ]

    def __init__(self, bus: EventBus, loop, workdir: str = "workspace") -> None:
        super().__init__()
        self.bus = bus
        self.loop = loop
        self.workdir = str(workdir)
        self.ui_state = UIState(
            mode=getattr(loop, "mode", "investigate"),
            provider=getattr(loop, "provider", "deepseek"),
            model=getattr(loop, "model", ""),
        )
        self._app_thread: int | None = None
        self._title_requested = False   # 每会话一次自动命名（session 切换时复位）
        self._tick_timer = None         # 状态栏 1s 定时器句柄（退出前显式停表，治本 flaky）

    @property
    def workdir_label(self) -> str:
        return Path(self.workdir).name

    @property
    def chat(self) -> ChatView:
        return self.query_one(ChatView)

    @property
    def mode_selector(self) -> ModeSelector:
        return self.query_one(ModeSelector)

    @property
    def composer(self) -> Composer:
        return self.query_one(Composer)

    @property
    def status_bar(self) -> StatusBar:
        return self.query_one(StatusBar)

    @property
    def activity(self) -> ActivityView:
        return self.query_one(ActivityView)

    @property
    def inspector(self) -> Inspector:
        return self.query_one(Inspector)

    @property
    def status_view(self) -> StatusView:
        return self.query_one(StatusView)

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield PhyScHeader()
            with Horizontal(id="main"):
                with TabbedContent(initial="tab-chat"):
                    with TabPane("CHAT", id="tab-chat"):
                        yield ChatView()
                    with TabPane("ACTIVITY", id="tab-activity"):
                        yield ActivityView()
                    with TabPane("STATUS", id="tab-status"):
                        yield StatusView()
                yield Inspector(id="inspector")
            yield Composer()
            yield StatusBar()

    def on_mount(self) -> None:
        self._app_thread = threading.get_ident()
        st = self.ui_state
        st.mode = getattr(self.loop, "mode", st.mode)
        st.provider = getattr(self.loop, "provider", st.provider)
        st.model = getattr(self.loop, "model", st.model)
        st.voice = getattr(self.loop, "voice", "academic")
        thinking = getattr(getattr(self.loop, "llm_client", None), "level", None)
        st.thinking_level = getattr(thinking, "value", "high")
        for kind in _SUBSCRIBED_KINDS:
            self.bus.subscribe(kind, self._on_bus_event)
        self.query_one("#composer-input", Input).focus()
        self._tick_timer = self.set_interval(1.0, self._tick_status)
        self._ensure_session()

    async def _close_all(self) -> None:
        """Textual 8.2.8 _shutdown 先 _close_all（清空 DOM）后 _close_messages
        （才停 Timer）：整秒 tick 落入两 await 之间窗口会撞已卸载的 StatusBar
        （NoMatches）。显式停表（治本）+ _tick_status 弱查询（兜底）双保险。
        """
        if self._tick_timer is not None:
            self._tick_timer.stop()
        await super()._close_all()

    def _ensure_session(self) -> None:
        """TUI 启动时创建会话并发布 session_changed（注入 ui_state，驱动自动命名）。"""
        store = getattr(getattr(self, "services", None), "session_store", None)
        if store is None:
            return
        sid = store.create_session(self.loop.mode)
        self.bus.publish(EVENT_SESSION_CHANGED, session_id=sid, title="")

    def on_resize(self, event) -> None:
        self._apply_responsive(event.size.width)

    def _apply_responsive(self, w: int | None = None) -> None:
        """五档响应式（UI_DESIGN §3.2，batch57 机制扩展）：Inspector 显隐与宽度、
        极简状态栏、Tab 切换条显隐。延续 Python 侧 size.width 检测，无 CSS 媒体查询。

        w 来自 on_resize 的 event.size.width（回调期间 self.size.width 滞后一拍，
        必须用 event 值）；None 时回退 self.size.width（供初始挂载调用）。
        """
        if w is None:
            w = self.size.width
        inspector = self.query_one_optional(Inspector)
        if inspector is None:
            return
        if w < _BP_COMPACT:
            inspector.display = False
        else:
            inspector.display = True
            inspector.styles.width = _INSPECTOR_WIDE if w >= _BP_WIDE else _INSPECTOR_NARROW
        sb = self.query_one_optional(StatusBar)
        if sb is not None:
            sb.set_minimal(w < _BP_MINIMAL)
        tabbed = self.query_one_optional(TabbedContent)
        if tabbed is not None:
            tabs = tabbed.query_one_optional(Tabs)
            if tabs is not None:
                tabs.display = w >= _BP_MINIMAL

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event) -> None:
        pane = getattr(event, "pane", None)
        if pane is not None and getattr(pane, "id", None) == "tab-status":
            self.status_view.refresh_content()

    def _tick_status(self) -> None:
        sb = self.query_one_optional(StatusBar)
        if sb is not None:
            sb.refresh_status()

    def _ui_call(self, fn) -> None:
        """跨线程安全回调：UI 线程直调，工作线程经 call_from_thread。"""
        if self._app_thread is not None and threading.get_ident() != self._app_thread:
            self.call_from_thread(fn)
        else:
            fn()

    def _on_bus_event(self, kind: str, payload: dict) -> None:
        def _apply() -> None:
            self.ui_state.handle(kind, payload)
            if kind == EVENT_MODE_CHANGED:
                self._refresh_mode_ui()
            elif kind == EVENT_AGENT_MESSAGE:
                self.chat.add_agent_message(payload.get("text", ""))
                self.status_bar.refresh_status()
            elif kind in (EVENT_TOOL_STARTED, EVENT_TOOL_SUCCEEDED, EVENT_TOOL_FAILED):
                self.chat.add_tool_card(kind, payload)
                if kind == EVENT_TOOL_SUCCEEDED and self.chat.gate_active:
                    self.chat.gate_tool_succeeded(
                        payload.get("name", ""), payload.get("summary", "")
                    )
                self.activity.add_event(kind, payload)
                self.status_bar.refresh_status()
            elif kind == EVENT_CACHE_HIT:
                self.chat.add_cache_line(payload.get("kind"), payload.get("score"))
                self.status_bar.flash_cache_hit()
                self.activity.add_event(kind, payload)
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
            elif kind == EVENT_CACHE_MISS:
                self.activity.add_event(kind, payload)
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
            elif kind == EVENT_THINKING_STARTED:
                self.chat.add_thinking_started(payload.get("level", "high"))
            elif kind == EVENT_THINKING_ENDED:
                self.chat.end_thinking(payload.get("level", ""), payload.get("text", ""))
            elif kind == EVENT_THINKING_CHUNK:
                if self.chat._thinking_cards:
                    self.chat._thinking_cards[-1].thinking_chunk(payload.get("text", ""))
            elif kind == EVENT_AGENT_CHUNK:
                self.chat.handle_agent_chunk(payload.get("text", ""))
            elif kind == EVENT_PAPER_FOUND:
                self.chat.add_paper(payload)
            elif kind == EVENT_GATE_STARTED:
                self.chat.add_gate_started(payload.get("question", ""))
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
                self.status_view.refresh_content()
            elif kind == EVENT_EVIDENCE_FOUND:
                self.activity.add_event(kind, payload)
                if self.chat.gate_active:
                    self.chat.gate_evidence_found(payload.get("count"))
                else:
                    self.inspector.set_evidence_success(payload.get("count"))
            elif kind == EVENT_ARTIFACT_CREATED:
                self.activity.add_event(kind, payload)
                self.inspector.add_artifact(payload.get("path", ""), payload.get("kind", "note"))
            elif kind == EVENT_TASK_PHASE_CHANGED:
                self.inspector.task_progress.update_phase(
                    payload.get("phase", ""),
                    payload.get("step", 0),
                    payload.get("total", 0),
                    payload.get("label", ""),
                    payload.get("steps"),
                )
                self.activity.add_event(kind, payload)
            elif kind == EVENT_AGENT_COMPLETED:
                if self.chat.gate_active:
                    self.chat.gate_agent_completed(payload)
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
            elif kind == EVENT_AGENT_INTERRUPTED:
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
                self.notify("STOPPED")
            elif kind in (EVENT_AGENT_STARTED, EVENT_CONTEXT_USAGE):
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
            elif kind == EVENT_ERROR:
                self.chat.add_system_line(f"错误：{payload.get('message', '')}")
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
            elif kind in (
                EVENT_SESSION_CHANGED,
                EVENT_MODEL_CHANGED,
                EVENT_VOICE_CHANGED,
                EVENT_THINKING_CHANGED,
            ):
                if kind == EVENT_SESSION_CHANGED:
                    self._title_requested = False  # 新会话：允许再次自动命名
                    self.chat.clear_history()  # 命名从当前会话真正第一条开始
                if kind in (EVENT_MODEL_CHANGED, EVENT_VOICE_CHANGED, EVENT_THINKING_CHANGED):
                    self.refresh_header()
                self.inspector.refresh_inspector()
                self.status_bar.refresh_status()
                self.status_view.refresh_content()

        self._ui_call(_apply)

    def _refresh_mode_ui(self) -> None:
        self.mode_selector.refresh_active(self.ui_state.mode)
        self.composer.refresh_badge(self.ui_state.mode)
        self.refresh_header()
        self.status_view.refresh_content()

    def refresh_header(self) -> None:
        self.query_one(PhyScHeader).refresh_header()

    def switch_mode(self, mode: str) -> None:
        """复用 cli.py 模式切换路径：loop.mode 赋值（上下文保留）→ 发布 mode_changed。"""
        if mode not in MODE_NAMES or mode == self.ui_state.mode:
            return
        self.loop.mode = mode
        self.bus.publish(EVENT_MODE_CHANGED, mode=mode)

    def submit_line(self, text: str) -> None:
        if text.startswith("/"):
            self.chat.add_user_message(text)
            self.dispatch_command(text)
        else:
            self.run_question(text)

    def dispatch_command(self, text: str) -> None:
        """斜杠命令触发与展示；命令实现复用 cli.py 既有 handler（框架主权在总控）。

        无确认类命令：StringIO console 调真 handler，输出上屏（add_system_line）。
        确认类（/cache clear）：notify 引导 --no-tui。/gate 走 run_question gate_round。
        """
        if text in ("/exit", "/quit"):
            self.exit()
            return
        if text in ("/plan", "/investigate", "/typeset"):
            self.switch_mode(text[1:])
            self.notify(f"Mode switched → {text[1:].upper()}")
            return
        if text == "/stop":
            if getattr(self.loop, "interrupt_event", None) is not None:
                self.loop.interrupt_event.set()
            self.notify("STOPPING…")
            return
        question = _gate_question(text)
        if question is not None:
            self.run_question(question, gate_round=True, echo=False)
            return
        if text.startswith("/gate"):
            self.notify("用法：/gate <问题>（在问题前加 /gate，本轮触发引用溯源校验）")
            return
        if text == "/help":
            self.action_help()
            return
        if text == "/sessions":
            self.action_session_list()
            return
        if text.startswith(("/model", "/provider")):
            self.action_model_picker()
            return
        if text == "/new":
            _handle_new(self.loop, self._svc_console())
            svc = getattr(self, "services", None)
            store = getattr(svc, "session_store", None) if svc is not None else None
            if store is not None:
                sid = store.create_session(self.loop.mode)
                self.bus.publish(EVENT_SESSION_CHANGED, session_id=sid, title="")
            self._reset_ui_for_new()
            self.notify("已开启新会话")
            return
        if text.startswith("/moa"):
            if self.ui_state.running:
                self.notify("任务处理中，/moa 请等待当前任务完成（并发会破坏上下文）")
                return
            svc = getattr(self, "services", None)
            if svc is None:
                self.notify(f"命令 {text} 暂不可用（services 未注入）")
                return
            client = svc.client
            buf = io.StringIO()

            def _moa_worker() -> None:
                try:
                    with contextlib.redirect_stdout(buf):
                        _handle_moa(self.loop, client, text)
                except Exception as exc:  # noqa: BLE001
                    buf.write(f"MoA 执行失败：{type(exc).__name__}: {exc}")
                out = buf.getvalue().strip()
                if out:
                    self._ui_call(lambda: self.chat.add_system_line(out))

            threading.Thread(target=_moa_worker, daemon=True).start()
            return
        if text.startswith("/dedup"):
            svc = getattr(self, "services", None)
            if svc is None:
                self.notify(f"命令 {text} 暂不可用（services 未注入）")
                return
            parsed = _parse_dedup(text)
            buf = io.StringIO()
            console = Console(file=buf, force_terminal=False, width=100)
            if parsed is None:
                console.print(
                    "用法：/dedup [--file <路径>] [--rewrite] <文本>"
                    "（--file 检测文件内容；--rewrite 附 AI 降重建议）"
                )
                self.chat.add_system_line(buf.getvalue().strip())
                return
            store = getattr(svc, "store", None)
            if store is None:
                self.notify("命令 /dedup 暂不可用（store 未注入）")
                return
            # 与 Rich 路径同线程执行：MemoryStore 连接线程亲和（sqlite 默认），
            # 放 worker 线程会撞 ProgrammingError；TUI 下索引量小，可接受同步执行
            try:
                _handle_dedup(
                    self.loop, svc.client, console, store, self.workdir, parsed
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]查重执行失败：{type(exc).__name__}: {exc}[/red]")
            out = buf.getvalue().strip()
            if out:
                self.chat.add_system_line(out)
            return
        svc = getattr(self, "services", None)
        if svc is None:
            self.notify(f"命令 {text} 暂不可用（services 未注入）")
            return
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False, width=100)
        try:
            if text.startswith("/cache"):
                if len(text.split()) > 1 and text.split()[1] == "clear":
                    self.notify("清空缓存请在 --no-tui 模式使用（需交互确认）")
                    return
                _handle_cache(None, console, svc.exact_cache, svc.semantic_cache, svc.embed_cache, text)
            elif text.startswith("/skill"):
                _handle_skill(console, svc.skill_metas, svc.loaded_skills, text)
            elif text.startswith("/mcp"):
                _handle_mcp(console, svc.mcp_registry, text)
            elif text.startswith("/thinking"):
                out = self._run_handler_captured(_handle_thinking, svc.client, text)
                if out:
                    self.chat.add_system_line(out)
                self.bus.publish(
                    EVENT_THINKING_CHANGED, level=str(svc.client.level.value)
                )
            elif text.startswith("/voice"):
                out = self._run_handler_captured(_handle_voice, self.loop, text)
                if out:
                    self.chat.add_system_line(out)
                self.bus.publish(EVENT_VOICE_CHANGED, voice=self.loop.voice)
            elif text.startswith("/schedule"):
                _handle_schedule(svc.scheduler, console, text)
            elif text.startswith(("/search", "/resume", "/fork")):
                self.notify("会话检索/恢复/分叉请用 /sessions 面板（Ctrl+L）")
                return
            else:
                self.notify(f"未知命令：{text}")
                return
        except Exception as exc:  # noqa: BLE001
            self.notify(f"命令执行失败：{exc}")
            return
        out = buf.getvalue().strip()
        if out:
            self.chat.add_system_line(out)

    def _svc_console(self) -> Console:
        """StringIO console 单例（/new 等直接 print 到 buffer 的命令用，输出上屏或弃用）。"""
        buf = getattr(self, "_svc_buf", None)
        if buf is None:
            buf = io.StringIO()
            self._svc_buf = buf
        return Console(file=buf, force_terminal=False, width=100)

    def _run_handler_captured(self, fn, *args) -> str:
        """捕获 print 型 handler 的 stdout 输出（/thinking /voice 上屏用）。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args)
        return buf.getvalue().strip()

    def _reset_ui_for_new(self) -> None:
        """/new UI 刷新（user_log_2 #6）：各组件回到"刚启动"状态。

        loop.context 已由 _handle_new reset；这里清 UI 侧缓存：
        消息区 DOM + 用户历史、Inspector 选中对象、task 面板、Activity、
        状态层 ctx/cache 计数、命名状态复位。不重启 loop 内存（证据/记忆保留）。
        """
        self.chat.reset_view()
        st = self.ui_state
        st.selected_object = None  # Inspector 回 RESEARCH CONTEXT（无选中对象）
        st.context_used = 0        # 状态栏读 UIState 缓存，需显式归零
        st.cache_hits = 0
        st.cache_misses = 0
        self.inspector.reset_task_panel()
        self.activity.clear()
        self.inspector.refresh_inspector()
        self.status_bar.refresh_status()
        self.status_view.refresh_content()
        self._title_requested = False  # 新会话允许再次自动命名

    def _fill_composer(self, text: str) -> None:
        """palette 选中命令填入 Composer（用户可见可改再 Enter，不直接执行）。"""
        inp = self.query_one("#composer-input", Input)
        inp.value = text
        inp.focus()

    def run_question(self, text: str, gate_round: bool = False, echo: bool = True) -> None:
        """本地回显 user 消息 + 子线程 loop.run() → agent_message 事件上屏。

        并发守卫：运行中 notify 提示 /stop 可中断（/stop 走 dispatch_command 不受此限）。
        中断复位：worker 每次 run 前 clear interrupt_event（Rich 路径 cli.py:1241
        同款保护，防上一轮 /stop 置位粘滞到下一轮）。gate_round 透传给 loop.run
        （/gate 前缀触发引用溯源校验轮）。echo=False 跳过本地回显（/gate 原文已由
        submit_line 回显，避免转译后问题双重上屏）。
        """
        if self.ui_state.running:
            self.notify("任务处理中，输入 /stop 可中断")
            return
        if echo:
            self.chat.add_user_message(text)
        self._maybe_auto_title()
        self.bus.publish(EVENT_AGENT_STARTED)
        self.ui_state.running = True
        ctx = getattr(self.loop, "context", None)
        build_msgs = getattr(ctx, "build_messages", None)
        before = len(build_msgs()) if build_msgs is not None else 0

        def worker() -> None:
            start = time.perf_counter()
            try:
                if getattr(self.loop, "interrupt_event", None) is not None:
                    self.loop.interrupt_event.clear()
                if gate_round:
                    answer = self.loop.run(text, gate_round=True)
                else:
                    answer = self.loop.run(text)
                interrupted = (
                    getattr(self.loop, "interrupt_event", None) is not None
                    and self.loop.interrupt_event.is_set()
                )
                if interrupted:
                    self.bus.publish(EVENT_AGENT_INTERRUPTED, reason="用户中断")
                else:
                    self.bus.publish(
                        EVENT_AGENT_COMPLETED, duration=time.perf_counter() - start
                    )
                self.bus.publish(EVENT_AGENT_MESSAGE, text=answer)
                self._persist_round(before)
            except Exception as exc:  # noqa: BLE001
                self.bus.publish(
                    EVENT_AGENT_COMPLETED, duration=time.perf_counter() - start
                )
                self.bus.publish(EVENT_ERROR, message=str(exc))
            finally:
                self.ui_state.running = False

        threading.Thread(target=worker, daemon=True).start()

    def _persist_round(self, before: int) -> None:
        """一轮完成后把新增消息落会话库（Rich 路径 cli.py 同款逻辑）。

        仅在有 session_store 且 session_id 有效时执行；落库失败静默（旁路
        功能，不影响本轮结果上屏）。resume/fork 依赖此处落库的数据。
        """
        store = getattr(getattr(self, "services", None), "session_store", None)
        sid = self.ui_state.session_id
        if store is None or not sid or sid == "new":
            return
        build_msgs = getattr(getattr(self.loop, "context", None), "build_messages", None)
        if build_msgs is None:
            return
        try:
            new_msgs = build_msgs()[before:]
            if new_msgs:
                store.append_round(sid, new_msgs)
        except Exception:  # noqa: BLE001 会话落库失败不阻断 UI（旁路）
            pass

    def _maybe_auto_title(self) -> None:
        """新会话首条用户消息后触发一次 flash 模型自动命名（失败静默）。

        触发条件：本会话未请求过 + 至少 1 条用户消息 + session_id 有效。
        命名在守护线程执行，不阻塞 UI；LLM 失败/无 store 均静默留空。
        命名来源取首条非斜杠命令消息（命令回显不进标题）；全为命令时回退首条。
        """
        history = getattr(self.chat, "user_history", None)
        if self._title_requested or not history:
            return
        sid = self.ui_state.session_id
        if not sid or sid == "new":
            return
        self._title_requested = True
        store = getattr(getattr(self, "services", None), "session_store", None)
        if store is None:
            return
        first = next((h for h in history if h and not h.startswith("/")), "")
        if not first:
            first = (history[0] or "")[:200]
        first = first[:200]

        def worker() -> None:
            self._title_worker(store, sid, first)

        threading.Thread(target=worker, daemon=True).start()

    def _title_worker(self, store, sid: str, first: str) -> None:
        """线程体：flash 模型起标题 → 写入 session_store；任何失败静默。"""
        try:
            resp = self.loop.llm_client.chat.completions.create(
                model=getattr(self.loop, "model", "deepseek-v4-flash"),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "根据首条用户消息，生成一个不超过12字的中文标题。"
                            "必须包含具体实体（材料/方法/现象/研究对象等），"
                            "禁止以'研究/调研/分析/对话/讨论'等泛词开头。"
                            "示例：'钙钛矿热降解机理综述'优于'钙钛矿研究'。"
                            f"直接输出标题，不要引号或解释。首条消息：{first}"
                        ),
                    }
                ],
                stream=False,
            )
            title = (resp.choices[0].message.content or "").strip()[:50]
            if title:
                store.set_title(sid, title)
        except Exception as exc:  # noqa: BLE001 命名是旁路功能，失败不阻断（dsh 核验：留 warning 便于排查）
            self._title_requested = False  # 本次失败复位：下一条消息可重试
            self.log.warning(f"auto-title failed: {type(exc).__name__}: {exc}")

    # ---- keymap.KEYMAP 动作 ----

    def action_switch_mode(self) -> None:
        composer = self.query_one(Composer)
        if composer.completion_active() and composer.accept_completion():
            return  # 补全框打开时 Tab 优先选中命令补全（用户拍板）
        idx = MODE_NAMES.index(self.ui_state.mode)
        self.switch_mode(MODE_NAMES[(idx + 1) % len(MODE_NAMES)])

    def action_switch_mode_prev(self) -> None:
        idx = MODE_NAMES.index(self.ui_state.mode)
        self.switch_mode(MODE_NAMES[(idx - 1) % len(MODE_NAMES)])

    def action_interrupt(self) -> None:
        self.dispatch_command("/stop")

    def action_scroll_bottom(self) -> None:
        self.chat.scroll_to_bottom()

    def action_close_overlay(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    def action_command_palette(self) -> None:
        self.push_screen(CommandPalette(self._fill_composer))

    def action_session_list(self) -> None:
        self.push_screen(SessionPicker())

    def action_model_picker(self) -> None:
        self.push_screen(ModelPicker())

    def action_help(self) -> None:
        self.push_screen(HelpModal())
