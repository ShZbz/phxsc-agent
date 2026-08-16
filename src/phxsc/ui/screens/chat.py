"""对话视图（batch57 骨架 + batch58 工具卡片混排 + 长任务进度）。

消息与工具卡片按事件到达时间序混排在 #messages 流中：user 消息右对齐
Static、agent 回答 Markdown、ToolCallCard 折叠卡片；长任务进度由 Inspector
下半区承载（batch65 移位，本视图不再挂 TaskProgress）。
滚动跟随：默认新消息自动回底；用户上滚暂停跟随并显示 `↓ new output`，
Ctrl+J（scroll_bottom）回底恢复跟随。

batch61 挂载（UI_DESIGN §6 五组件接线）：ThinkingBlock / PaperCard / GateFlow
由 ChatView 持有并 lazy 创建，事件经 app.py `_on_bus_event` 调本类方法挂进
消息流尾部；各自引用列表复用 batch59a 的 200 上限规则。cache_hit 命中在答案
前插入 `⚡ semantic cache · 0.96` 行（§6.4）。
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import MouseScrollDown, MouseScrollUp
from textual.widget import Widget
from textual.widgets import Markdown, Static

from phxsc.ui.events import (
    EVENT_TOOL_FAILED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
)
from phxsc.ui.theme import TOKENS
from phxsc.ui.widgets.gate_flow import GateFlow
from phxsc.ui.widgets.paper_result import PaperCard
from phxsc.ui.widgets.thinking_block import ThinkingBlock
from phxsc.ui.widgets.tool_card import ToolCallCard

# 消息流挂载上限（batch59a 规则：≥200 时 pop(0).remove()）
_CARD_CAP = 200


class ChatView(Widget):
    """CHAT 主视图：消息/工具卡片流 + 上滚暂停跟随。"""

    DEFAULT_CSS = f"""
    ChatView {{
        height: 1fr;
    }}
    #messages {{
        height: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
    }}
    .msg-user {{
        color: {TOKENS["text1"]};
        text-align: right;
        padding: 0 1;
    }}
    .msg-agent {{
        margin: 0 0;
    }}
    .msg-cache {{
        color: {TOKENS["warning"]};
    }}
    .msg-empty {{
        color: {TOKENS["text3"]};
        padding: 1 2;
    }}
    #chat-scroll-hint {{
        height: 1;
        color: {TOKENS["warning"]};
        display: none;
    }}
    """

    def __init__(self) -> None:
        super().__init__()
        self._scroll: VerticalScroll | None = None
        self._hint: Static | None = None
        self._empty: Static | None = None  # 首启空态行（有消息即隐藏）
        self._paused = False
        self._cards: list[ToolCallCard] = []
        self._thinking_cards: list[ThinkingBlock] = []
        self._paper_cards: list[PaperCard] = []
        self._gate_cards: list[GateFlow] = []
        self._gate_active = False
        self._user_history: list[str] = []
        self._stream_block: Static | None = None  # 流式回答块（None=无活跃块）
        self._stream_text: str = ""  # 流式块累积纯文本

    def compose(self) -> ComposeResult:
        self._scroll = VerticalScroll(id="messages")
        yield self._scroll
        self._hint = Static("↓ new output（Ctrl+J 回底）", id="chat-scroll-hint")
        yield self._hint

    def on_mount(self) -> None:
        self._empty = Static("输入问题开始；/ 命令，? 帮助", classes="msg-empty")
        self._scroll.mount(self._empty)

    def _hide_empty(self) -> None:
        if self._empty is not None:
            self._empty.remove()
            self._empty = None

    def add_user_message(self, text: str) -> None:
        self._hide_empty()
        self._user_history.append(text)
        self._scroll.mount(Static(text, classes="msg-user", markup=False))
        self._maybe_scroll_bottom()

    @property
    def user_history(self) -> list[str]:
        return self._user_history

    def clear_history(self) -> None:
        """清空用户消息历史（/new 或会话切换时调用——命名只取当前会话消息）。"""
        self._user_history.clear()

    def reset_view(self) -> None:
        """消息区 UI 归零（/new）：清 DOM + 卡片引用 + 用户历史 + 滚动暂停态。"""
        if self._scroll is not None:
            self._scroll.remove_children()
            self._empty = Static("输入问题开始；/ 命令，? 帮助", classes="msg-empty")
            self._scroll.mount(self._empty)
        self._cards.clear()
        self._thinking_cards.clear()
        self._paper_cards.clear()
        self._gate_cards.clear()
        self._gate_active = False
        self._stream_block = None
        self._stream_text = ""
        self.clear_history()
        self._paused = False
        if self._hint is not None:
            self._hint.display = False

    def handle_agent_chunk(self, text: str) -> None:
        """agent_chunk 增量：首个 chunk 挂纯文本 Static（关闭 markup），后续追加刷新。"""
        if not text:
            return
        if self._stream_block is None:
            self._stream_block = Static(text, classes="msg-agent", markup=False)
            self._stream_text = text
            self._scroll.mount(self._stream_block)
        else:
            self._stream_text += text
            self._stream_block.update(self._stream_text)
        self._maybe_scroll_bottom()

    def add_agent_message(self, text: str) -> None:
        """agent_message 到达：移除流式块（如有）后挂 Markdown——先移除后渲染。"""
        self._hide_empty()
        if self._stream_block is not None:
            self._stream_block.remove()
            self._stream_block = None
            self._stream_text = ""
        self._scroll.mount(Markdown(text, classes="msg-agent"))
        self._maybe_scroll_bottom()

    def add_system_line(self, text: str) -> None:
        """命令/系统输出行（关闭 markup，dim 样式；batch60 dispatch 上屏）。"""
        self._hide_empty()
        self._scroll.mount(Static(text, classes="msg-system", markup=False))
        self._maybe_scroll_bottom()

    def add_tool_card(self, kind: str, payload: dict) -> None:
        """工具事件 → 对话流插入/更新 ToolCallCard（与消息按时间序混排）。"""
        if kind == EVENT_TOOL_STARTED:
            card = ToolCallCard(payload.get("name", ""), payload.get("args", ""))
            self._scroll.mount(card)
            if len(self._cards) >= 200:
                self._cards.pop(0).remove()
            self._cards.append(card)
        else:
            card = self._find_running(payload.get("name"))
            if card is None:
                card = ToolCallCard(payload.get("name", ""))
                self._scroll.mount(card)
                if len(self._cards) >= 200:
                    self._cards.pop(0).remove()
                self._cards.append(card)
            if kind == EVENT_TOOL_SUCCEEDED:
                card.succeed(
                    payload.get("name", ""),
                    payload.get("duration"),
                    payload.get("summary", ""),
                )
            elif kind == EVENT_TOOL_FAILED:
                card.fail(
                    payload.get("name", ""),
                    payload.get("error", ""),
                    payload.get("reason", ""),
                    payload.get("fix_hint", ""),
                )
        self._maybe_scroll_bottom()

    def _find_running(self, name) -> ToolCallCard | None:
        for card in reversed(self._cards):
            if card.status == "running" and (not name or card.tool_name == name):
                return card
        return None

    # ---- batch61 五组件挂载（thinking/paper/gate/cache）----

    @property
    def gate_active(self) -> bool:
        """gate 轮进行中标志：gate_started 置位、agent_completed 清除。"""
        return self._gate_active

    def _mount_capped(self, widget, lst) -> None:
        """挂到消息流尾部并套 200 上限（最老项 remove）。"""
        self._hide_empty()
        self._scroll.mount(widget)
        lst.append(widget)
        if len(lst) > _CARD_CAP:
            lst.pop(0).remove()

    def add_thinking_started(self, level: str) -> ThinkingBlock:
        """thinking_started → 新建 ThinkingBlock 挂尾部（折叠一行 reasoning · level）。"""
        card = ThinkingBlock(level=level)
        self._mount_capped(card, self._thinking_cards)
        card.thinking_started(level)
        self._maybe_scroll_bottom()
        return card

    def end_thinking(self, level: str = "", text: str = "") -> None:
        """thinking_ended → 最近 thinking 卡收尾（结束计时，保持折叠行可见）。"""
        if not self._thinking_cards:
            return
        self._thinking_cards[-1].thinking_ended(level, text)
        self._maybe_scroll_bottom()

    def add_paper(self, payload: dict) -> PaperCard:
        """paper_found → 新建 PaperCard 挂尾部并注入结构化条目。"""
        card = PaperCard()
        self._mount_capped(card, self._paper_cards)
        card.add_from_event(payload)
        self._maybe_scroll_bottom()
        return card

    def add_gate_started(self, question: str) -> GateFlow:
        """gate_started → 新建 GateFlow 挂尾部，置 gate_active。"""
        card = GateFlow()
        self._mount_capped(card, self._gate_cards)
        self._gate_active = True
        card.gate_started(question)
        self._maybe_scroll_bottom()
        return card

    def gate_evidence_found(self, count) -> None:
        """gate 轮 evidence_found → 最近 gate 卡推进步骤。"""
        if not self._gate_cards:
            return
        self._gate_cards[-1].evidence_found(count)

    def gate_tool_succeeded(self, name: str = "", summary: str = "") -> None:
        """gate 轮 tool_succeeded → 最近 gate 卡推进步骤（仅 gate_active 时由 app 调用）。"""
        if not self._gate_cards:
            return
        self._gate_cards[-1].tool_succeeded(name, summary)

    def gate_agent_completed(self, payload: dict) -> None:
        """gate 轮 agent_completed → 最近 gate 卡收尾，清除 gate_active。"""
        if not self._gate_cards:
            return
        self._gate_cards[-1].agent_completed(payload)
        self._gate_active = False

    def add_cache_line(self, kind: str, score=None) -> None:
        """cache_hit → 答案前插入 `⚡ semantic cache · 0.96` / `↻ exact cache`。"""
        if kind == "exact":
            text = "↻ exact cache"
        elif kind == "prefix":
            text = "↻ prefix cache"
        else:
            suffix = f" · {score:.2f}" if isinstance(score, (int, float)) else ""
            text = f"⚡ semantic cache{suffix}"
        self._scroll.mount(Static(text, classes="msg-cache", markup=False))
        self._maybe_scroll_bottom()

    def _maybe_scroll_bottom(self) -> None:
        if self._paused:
            self._hint.display = True
        else:
            self._scroll.scroll_end(animate=False)
            self._hint.display = False

    def scroll_to_bottom(self) -> None:
        self._paused = False
        self._hint.display = False
        self._scroll.scroll_end(animate=False)

    @on(MouseScrollUp)
    def _on_scroll_up(self, event: MouseScrollUp) -> None:
        if self._is_over_messages(event) and self._scroll.scroll_y > 0:
            self._paused = True
            self._hint.display = True

    @on(MouseScrollDown)
    def _on_scroll_down(self, event: MouseScrollDown) -> None:
        if self._is_over_messages(event) and self._scroll.scroll_y >= self._scroll.max_scroll_y:
            self._paused = False
            self._hint.display = False

    def _is_over_messages(self, event) -> bool:
        node = event.widget
        while node is not None:
            if node is self._scroll:
                return True
            node = node.parent
        return False
