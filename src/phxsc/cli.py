"""PhySc-agent CLI 入口。

用法：python -m phxsc.cli [--mode MODE] [--workdir DIR] [--model MODEL]

交互式会话：输入问题回车交给 AgentLoop；/exit 退出；/plan /investigate
/typeset 切换模式（一行 loop.mode 赋值，上下文保留不重建）。DEEPSEEK_API_KEY
从环境变量读取。
"""

import os as _os
import sys as _sys

_splash = None
if _sys.stdout.isatty() and _sys.stdin.isatty() and not _os.environ.get("PHXSC_NO_SPLASH"):
    from phxsc.splash import start_splash
    _splash = start_splash()

import argparse
import os
import re
import shlex
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import CompleteEvent, Completer, WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.menus import CompletionsMenuControl
from rich.console import Console
from rich.table import Table

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.loop import AgentLoop
from phxsc.agent.moa import run_moa
from phxsc.agent.modes import BASE_SYSTEM_PROMPT, MODE_NAMES
from phxsc.agent.thinking import PROVIDER_DEEPSEEK, ThinkingLevel, build_thinking_params, build_thinking_top
from phxsc.agent.tools import Tool, ToolRegistry, tool
from phxsc.cache.embed_cache import EmbedCache
from phxsc.cache.exact import ExactCache
from phxsc.cache.semantic import SemanticCache
from phxsc.gates.citation import create_gate
from phxsc.mcp.config import load_config
from phxsc.mcp.registry import McpRegistry
from phxsc.memory.hybrid import hybrid_retrieve
from phxsc.memory.inject import build_injection
from phxsc.memory.store import MemoryStore
from phxsc.sandbox.paths import safe_read_path, safe_write_path
from phxsc.scheduler.jobs import create_scheduler
from phxsc.sessions import SessionStore, _default_sessions_db_path
from phxsc.skills.loader import load_skill_body
from phxsc.skills.scan import build_metadata_table, scan_skills
from phxsc.ui.events import (
    EVENT_ARTIFACT_CREATED,
    EVENT_EVIDENCE_FOUND,
    EVENT_PAPER_FOUND,
    EVENT_TOOL_FAILED,
    EVENT_TOOL_STARTED,
    EVENT_TOOL_SUCCEEDED,
    EventBus,
)
from phxsc.providers import DEFAULT_PROVIDER, ProviderKeyError, all_providers, build_client
from phxsc.settings import (
    load_model,
    load_moa_workers,
    load_provider,
    load_settings,
    save_settings,
)
from phxsc.telemetry import Telemetry
from phxsc.tools.arxiv import arxiv_search
from phxsc.tools import dedup as dedup_tools
from phxsc.tools.dedup import dedup_rewrite, plagiarism_check
from phxsc.tools.lineage import lineage_track
from phxsc.tools.lineage_view import lineage_view
from phxsc.tools.memory import _get_embedder, _get_store, memory_search, remember
from phxsc.tools.notes import notes_list, notes_read, notes_write
from phxsc.tools.oa import oa_download
from phxsc.tools.paper import paper_download
from phxsc.tools.pdf import clean_surrogates, pdf_parse
from phxsc.tools.plan import plans_read, plan_write
from phxsc.tools.scihub import scihub_download
from phxsc.tools.typeset import typeset_generate, typeset_pdf
from phxsc.tools.vision import figure_analyze
from phxsc.tools.web import web_search, web_search_api
from phxsc.tools.zotero import zotero_list_recent, zotero_status

DEFAULT_MODEL = "deepseek-v4-flash"
MAX_STEPS = 15

# 注入轨总量警告阈值（8KB）：超阈值时 /skill load 打黄色警告，不阻断
SKILL_WARN_BYTES = 8192

# / 命令自动补全表（输入 / 前缀时弹出）
SLASH_COMMANDS = (
    "/plan",
    "/investigate",
    "/typeset",
    "/new",
    "/gate",
    "/moa",
    "/dedup",
    "/thinking",
    "/voice",
    "/schedule",
    "/cache",
    "/skill",
    "/mcp",
    "/sessions",
    "/search",
    "/resume",
    "/fork",
    "/model",
    "/provider",
    "/stop",
    "/help",
    "/exit",
    "/quit",
)

# 上下文占用估算窗口（tokens）：粗估 ContextConfig.max_window(20) * 2000 tokens/轮。
# DeepSeek tokenizer 未公开，字符数//4 只是量级估算，进度条仅作直观参考。
# DeepSeek V4-Flash 上下文窗口（官方 1M tokens）
CONTEXT_WINDOW_TOKENS = 1_000_000
_BAR_WIDTH = 5


class _UIState:
    """bottom_toolbar 每次渲染读取的会话状态（loop 引用 + 计时 + 忙碌标志）。"""

    def __init__(self, loop: "AgentLoop", start_time: float) -> None:
        self.loop = loop
        self.start_time = start_time  # perf_counter，CLI 启动时刻
        self.busy: bool = False
        self.turn_start: float | None = None  # perf_counter，本轮开始时刻
        self.last_turn_seconds: float | None = None  # 上一轮耗时（秒）


class _CommandCompleter(Completer):
    """仅对以 / 开头的输入补全命令；普通输入返回空，不弹补全、不干扰打字。

    sentence=True 使 word_before_cursor 取整个已输入文本，保证 /he → /help
    正确匹配（默认 WORD 分词会把 / 当作词边界）。
    """

    def __init__(self, commands: Sequence[str] = SLASH_COMMANDS) -> None:
        self._inner = WordCompleter(list(commands), sentence=True)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> list:
        if not document.text_before_cursor.startswith("/"):
            return []
        return list(self._inner.get_completions(document, complete_event))


def _completion_menu_open() -> bool:
    """当前 buffer 的补全菜单是否打开（complete_state 非 None）。

    作为滚动绑定 filter 的条件项：菜单未打开时绑定不命中，down/up 交还
    默认行为（光标移动/历史导航），不破坏普通输入。无 app 会话时视为未打开。
    """
    try:
        app = get_app()
        buffer = app.current_buffer
    except Exception:  # noqa: BLE001  无 app 会话（直接调用场景）视为未打开
        return False
    return buffer is not None and buffer.complete_state is not None


# 补全菜单可视区跟随除数：选中项越过可视区约 1/3 高度后，可视区逐行跟随
# 滚动（选中项保持在可视区上 1/3 位置附近），两端夹紧不越界。
_COMPLETION_FOLLOW_DIVISOR = 3


def _completion_follow_scroll(
    complete_index: int, item_count: int, window_height: int
) -> int:
    """选中项移动后可视区应处的 scroll（content 行号）：idx - h//3，夹紧 [0, n-h]。

    prompt_toolkit 的 CompletionsMenu 自带滚动仅当选中项越出可视区边缘才发生
    （构造参数 scroll_offset=1 语义），用户实测"选中指示下移但可视区不滚动"；
    batch75 改为显式驱动：选中项保持在可视区上 1/3 高度处，可视区随选中
    逐行滚动（滚出旧项/滚入新项），顶部/底部夹紧不回滚越界。
    """
    if window_height <= 0 or window_height >= item_count:
        return 0
    follow = complete_index - window_height // _COMPLETION_FOLLOW_DIVISOR
    return max(0, min(follow, item_count - window_height))


def _scroll_completion_menu_follow(event) -> None:
    """down/up 移动选中项后显式驱动补全菜单可视区滚动跟随（batch75）。

    依据：prompt_toolkit 3.0.53 无 CompletionState.scroll_offset 公开属性，
    CompletionsMenu 的 scroll_offset 仅构造期生效；其渲染滚动状态是菜单
    Window 的 vertical_scroll（下帧渲染以该值为起点并保持选中行可见）。
    故在选中移动后直接设置该值实现"调用菜单滚动方法"等价路径。菜单未打开、
    无 app 会话（直接调用 handler）、非单列菜单或菜单尚未渲染时静默跳过，
    交还 prompt_toolkit 默认滚动（选中项越界时仍自动保持可见）。
    """
    app = getattr(event, "app", None)
    if app is None:
        return
    state = event.current_buffer.complete_state
    if state is None or state.complete_index is None or not state.completions:
        return
    try:
        windows = app.layout.visible_windows
    except Exception:  # noqa: BLE001  布局不可用时不驱动
        return
    for window in windows:
        if not isinstance(getattr(window, "content", None), CompletionsMenuControl):
            continue
        render_info = getattr(window, "render_info", None)
        if render_info is None:
            return  # 菜单尚未渲染过：交还默认滚动逻辑
        window.vertical_scroll = _completion_follow_scroll(
            state.complete_index, len(state.completions), render_info.window_height
        )
        return


def _completion_scroll_bindings() -> KeyBindings:
    """补全菜单打开时接管 down/up：逐项移动并随菜单滚动，两端才回绕。

    默认绑定对 down/up 恒命中；注入本绑定后（PromptSession 用户绑定优先级
    高于默认），菜单打开时走本绑定：中间项 complete_next/complete_previous
    逐项下移（显式驱动菜单可视区滚动跟随），末项后再 down 回绕到第一条、
    首项上 up 回绕到最后一条——禁止提前回绕。菜单未打开时 filter 不命中，
    down/up 走默认行为。
    """
    kb = KeyBindings()

    @kb.add("down", filter=has_focus(DEFAULT_BUFFER) & Condition(_completion_menu_open))
    def _down(event) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            return
        if state.complete_index is None:
            buffer.go_to_completion(0)
        elif state.complete_index == len(state.completions) - 1:
            buffer.go_to_completion(0)  # 最后一条后再 down：回绕到第一条
        else:
            buffer.complete_next()
        _scroll_completion_menu_follow(event)

    @kb.add("up", filter=has_focus(DEFAULT_BUFFER) & Condition(_completion_menu_open))
    def _up(event) -> None:
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None or not state.completions:
            return
        if state.complete_index is None:
            buffer.go_to_completion(len(state.completions) - 1)
        elif state.complete_index == 0:
            buffer.go_to_completion(len(state.completions) - 1)  # 首条上 up：回绕到最后一条
        else:
            buffer.complete_previous()
        _scroll_completion_menu_follow(event)

    return kb


def _format_duration(seconds: float | None) -> str:
    """秒数 → 友好时长：<60s 显示 x.xs，否则 m:ss / h:mm:ss；None 显示 —。"""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _estimate_context_tokens(loop: "AgentLoop") -> int:
    """当前 messages 粗估 token（字符数//4；DeepSeek tokenizer 未公开）。"""
    return sum(len(str(m)) // 4 for m in loop.context.build_messages())


def _progress_bar(used: int, total: int = CONTEXT_WINDOW_TOKENS) -> str:
    """固定宽度进度条，如 [██░░░] 40%（超过上限截为 100%）。"""
    pct = min(1.0, used / total) if total else 0.0
    filled = int(pct * _BAR_WIDTH)
    return f"[{'█' * filled}{'░' * (_BAR_WIDTH - filled)}] {pct * 100:.0f}%"


def _mode_label(mode: str) -> str:
    """模式标识（PhySc 独有：状态栏最前段，彩色区分）。"""
    colors = {"plan": "cyan", "investigate": "green", "typeset": "magenta"}
    color = colors.get(mode, "white")
    return f"[{color}][{mode}][/{color}]"


def _thinking_effort_label(loop: "AgentLoop") -> str:
    """thinking 档位段：读 loop.llm_client（ThinkingLLM）的 level，缺省按默认档 high。"""
    client = getattr(loop, "llm_client", None)
    level = getattr(client, "level", None)
    value = getattr(level, "value", ThinkingLevel.HIGH.value)
    return f"reasoning effort:{value}"


def _render_toolbar(state: _UIState) -> str:
    """状态栏单行文本：[模式] │ thinking 档位 │ 状态 │ 模型 │ 上下文 │ 服务端命中率 │ 本轮 │ 总时长 │ 时钟。

    模式标识钉死在最前（PhySc 独有，不受渐进披露影响）；
    纯函数：每次渲染读取最新 loop.stats()/计时，无副作用。
    """
    stats = state.loop.stats()
    status = "⚙ working" if state.busy else "ready"
    # ctx 口径与 TUI 一致（U5）：优先 loop.last_usage 真实 prompt_tokens，无则 ~ 估算
    last_usage = getattr(state.loop, "last_usage", None) or {}
    prompt_tokens = last_usage.get("prompt_tokens") or 0
    if prompt_tokens > 0:
        used, prefix = prompt_tokens, ""
    else:
        used, prefix = _estimate_context_tokens(state.loop), "~"
    total = (
        state.loop._context_window()
        if hasattr(state.loop, "_context_window")
        else CONTEXT_WINDOW_TOKENS
    )
    ctx = f"{prefix}{used}/{total} {_progress_bar(used, total)}"
    segs = [
        _mode_label(stats["mode"]),
        _thinking_effort_label(state.loop),
        status,
        f"{stats['provider']}/{stats['model']}",
        ctx,
    ]
    if stats["prefix_hit_tokens"] + stats["prefix_miss_tokens"]:
        segs.append(f"命中 {stats['prefix_hit_rate'] * 100:.0f}%")
    if state.busy and state.turn_start is not None:
        turn = time.perf_counter() - state.turn_start
    else:
        turn = state.last_turn_seconds
    total = time.perf_counter() - state.start_time
    segs.extend(
        [
            f"本轮 {_format_duration(turn)}",
            f"总 {_format_duration(total)}",
            datetime.now().strftime("%H:%M"),
        ]
    )
    return " │ ".join(segs)


def _build_toolbar(state: _UIState) -> Callable[[], str]:
    """构造 bottom_toolbar 回调（返回单行字符串，每次渲染读最新状态）。"""

    def render() -> str:
        return _render_toolbar(state)

    return render


def _build_session(state: _UIState) -> PromptSession:
    """构造 PromptSession：/ 命令自动补全 + 补全菜单滚动绑定 + 常驻底部状态栏。"""
    return PromptSession(
        completer=_CommandCompleter(),
        bottom_toolbar=_build_toolbar(state),
        complete_while_typing=True,
        key_bindings=_completion_scroll_bindings(),
    )


def _input_line(session: PromptSession | None, prompt: str) -> str:
    """读一行输入：tty 用 PromptSession（补全/历史/方向键），非 tty fallback input()。"""
    if session is not None:
        return session.prompt(prompt)
    return input(prompt)


def _cached_memory_search_tool(embed_cache: EmbedCache) -> Tool:
    """带 query 向量缓存的 memory_search 工具（schema 与原始一致，注入 EmbedCache）。

    loop 不持有 embedder/store（在 tools/memory.py 模块级单例），hybrid_retrieve 的
    调用链在 tools/memory.py 内；这里用同参数 schema 的包装替换已注册工具，
    把 EmbedCache 传入 hybrid_retrieve，不改动 tools 模块本体。
    """
    def cached(query: str, top_k: int = 5) -> str:
        hits = hybrid_retrieve(_get_store(), _get_embedder(), query, top_k, cache=embed_cache)
        if not hits:
            return "未找到相关记忆"
        return "\n".join(f"- [{m['type']}] {m['content']} ({m['score']:.3f})" for m in hits)

    return Tool(
        name=memory_search.name,
        description=memory_search.description,
        fn=cached,
        mode=memory_search.mode,
        parameters=memory_search.parameters,
    )


def _skill_load_tool(metas) -> Tool:
    """包装 skill_load 工具：按 name 加载技能正文（含资源清单）。

    缓存经济学铁律：技能正文走工具返回（区2），绝不进 system prompt（区1）。
    未命中返回结构化错误 {error, reason, fix_hint}，不抛给上层。
    """
    def skill_load(name: str) -> str:
        body = load_skill_body(name, metas)
        if body is None:
            return {
                "error": f"技能 {name!r} 不存在或加载失败",
                "reason": "skill_not_found",
                "fix_hint": "先 /skill list 查看可用技能",
            }
        lines = [body.content]
        if body.resources:
            lines.append("资源文件：" + "、".join(body.resources))
        return "\n".join(lines)

    return tool(
        name="skill_load",
        description="按技能名加载可用技能全文（含 resources 资源清单），技能名见 system prompt 元数据表",
        mode="*",
    )(skill_load)


def _register_tools(
    registry: ToolRegistry,
    embed_cache: EmbedCache | None = None,
    skill_metas: list | None = None,
) -> ToolRegistry:
    """集中注册工具：后续新增工具在此追加；embed_cache/skill_metas 非空时注入对应包装。"""
    registry.register_all(
        [
            arxiv_search,
            lineage_track,
            lineage_view,
            web_search,
            web_search_api,
            figure_analyze,
            memory_search,
            remember,
            pdf_parse,
            paper_download,
            oa_download,
            scihub_download,
            zotero_status,
            zotero_list_recent,
            notes_write,
            notes_read,
            notes_list,
            plan_write,
            plans_read,
            typeset_generate,
            typeset_pdf,
            plagiarism_check,
            dedup_rewrite,
        ]
    )
    if embed_cache is not None:
        registry.register(_cached_memory_search_tool(embed_cache))
    if skill_metas is not None:
        registry.register(_skill_load_tool(skill_metas))
    return registry


class ThinkingLLM:
    """OpenAI 兼容包装：按档位注入 extra_body 控制 DeepSeek thinking。"""

    def __init__(self, inner, provider: str = PROVIDER_DEEPSEEK,
                 level: ThinkingLevel = ThinkingLevel.HIGH):
        self._inner = inner
        self._provider = provider
        self._level = level

    def set_inner(self, inner) -> None:
        """运行时换底层 client（切换 provider 用；引用不变，loop 自动生效）。"""
        self._inner = inner

    def set_provider(self, provider: str) -> None:
        """运行时换 provider 名（影响 thinking 注入形态）。"""
        self._provider = provider

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    @property
    def level(self) -> ThinkingLevel:
        return self._level

    def set_level(self, level: ThinkingLevel) -> None:
        self._level = level

    def create(self, **kwargs):
        params = build_thinking_params(self._provider, self._level)
        if params:
            kwargs["extra_body"] = params
        top = build_thinking_top(self._provider, self._level)
        if top:
            kwargs.update(top)
        return self._inner.chat.completions.create(**kwargs)


class _PrintingRegistry(ToolRegistry):
    """打印工具调用过程的 Registry 包装（Rich 打印工具名 + 耗时；清洗非法 surrogate）。

    bus 非空时（TUI）按 batch56 契约额外发布工具事件：tool_started /
    tool_succeeded / tool_failed；bus=None（Rich/非 tty）行为与现状完全一致。
    """

    def __init__(self, console: Console, bus: EventBus | None = None) -> None:
        super().__init__()
        self._console = console
        self._bus = bus

    def call(self, name: str, args: dict):
        summary = self._summarize_args(args)
        if self._bus is not None:
            self._bus.publish(EVENT_TOOL_STARTED, name=name, args=summary)
        start = time.perf_counter()
        result = super().call(name, args)
        raw_dur = time.perf_counter() - start
        dur = _format_duration(raw_dur)
        if self._bus is None:
            self._console.print(f"[cyan]→ 调用工具 {name}{summary} ({dur})[/cyan]")
        if isinstance(result, dict) and "error" in result:
            err = clean_surrogates(str(result["error"]))
            if self._bus is None:
                self._console.print(f"[red]⚠ 工具 {name} 失败 ({dur})：{err}[/red]")
            hint = result.get("fix_hint")
            if hint:
                if self._bus is None:
                    self._console.print(f"[yellow]  提示：{clean_surrogates(str(hint))}[/yellow]")
            if self._bus is not None:
                self._bus.publish(
                    EVENT_TOOL_FAILED,
                    name=name,
                    error=err,
                    reason=clean_surrogates(str(result.get("reason") or "")),
                    fix_hint=clean_surrogates(str(hint or "")),
                )
        else:
            if self._bus is not None:
                summary_text = (
                    clean_surrogates(str(result))[:80] if result is not None else ""
                )
                self._bus.publish(
                    EVENT_TOOL_SUCCEEDED,
                    name=name,
                    duration=raw_dur,
                    summary=summary_text,
                )
            if self._bus is not None:
                if name == "pdf_parse":
                    m = re.search(r"evidence (\d+) 条", str(result))
                    self._bus.publish(EVENT_EVIDENCE_FOUND, count=int(m.group(1)) if m else None)
                elif name in ("paper_download", "oa_download", "scihub_download"):
                    self._bus.publish(
                        EVENT_PAPER_FOUND,
                        title=str(args.get("source_id") or args.get("doi") or ""),
                        journal=None, year=None, relevance=None,
                    )
                elif name in ("notes_write", "plan_write", "typeset_generate", "typeset_pdf"):
                    _kind = {"notes_write": "note", "plan_write": "plan",
                             "typeset_generate": "typeset", "typeset_pdf": "typeset"}[name]
                    self._bus.publish(
                        EVENT_ARTIFACT_CREATED,
                        payload={"path": str(args.get("title", "")), "kind": _kind},
                    )
        return result

    @staticmethod
    def _summarize_args(args: dict) -> str:
        """参数摘要：每个参数取首字段值，总长 ≤60 字符；清洗非法 surrogate。"""
        if not args:
            return ""
        parts = []
        for key, value in args.items():
            text = clean_surrogates(str(value))
            if len(text) > 24:
                text = text[:24] + "…"
            parts.append(f"{key}={text}")
        joined = " ".join(parts)
        if len(joined) > 60:
            joined = joined[:60] + "…"
        return f" · {joined}"


def _project_root() -> Path:
    """项目根目录（src/phxsc/cli.py 向上三级）。"""
    return Path(__file__).resolve().parents[2]


def _default_env_path() -> str:
    """默认 .env 路径：<项目根>/.env。"""
    return str(_project_root() / ".env")


def _resolve_workdir_arg(workdir_arg: str | None) -> str:
    """--workdir 未显式传入（None）时，PHXSC_WORKDIR 环境变量优先。"""
    if workdir_arg is None:
        return os.environ.get("PHXSC_WORKDIR") or "workspace/"
    return workdir_arg or "workspace/"


def _resolve_workdir(workdir_arg: str) -> str:
    """把 --workdir 解析为绝对路径；不存在则创建；过 sandbox 校验。"""
    raw = workdir_arg
    if not os.path.isabs(raw):
        raw = os.path.join(str(_project_root()), raw)
    os.makedirs(raw, exist_ok=True)
    try:
        return safe_write_path("", raw)
    except ValueError as exc:
        print(f"错误：workdir 不合法 - {exc}", file=sys.stderr)
        raise SystemExit(1)


def _build_loop(
    client,
    registry: ToolRegistry,
    mode: str,
    workdir: str,
    model: str,
    provider: str = "deepseek",
    gate=None,
    store=None,
    telemetry=None,
    cache=None,
    semantic_cache=None,
    embed_cache=None,
    skills_table: str = "",
    loaded_skills: dict[str, str] | None = None,
) -> AgentLoop:
    """组装 ContextManager + AgentLoop；workdir 挂到 context 备用。

    单上下文常驻架构：system prompt 固定为 BASE_SYSTEM_PROMPT（三模式合并
    说明），tools schema 全量（all_tools()）；模式通过每轮 user 首行
    [mode: xxx] 动态注入、权限由 registry.can_call 在工具调用时强制。
    切模式只改 loop.mode，上下文永不重建。传入 store 时把重要记忆注入片段
    （build_injection）追加到 system_prompt 末尾（\\n\\n 分隔）；为空则不注入。
    skills_table（元数据表，scan_skills+build_metadata_table 组装结果）同样
    追加到 system_prompt 末尾：缓存经济学铁律——元数据表只进 system prompt
    （区1，此处启动组装一次，前缀字节稳定）；skill 正文只走 user 消息/工具
    返回（区2，经 loop.loaded_skills 的 [skills] 段），任何动态内容不进前缀。
    只在启动组装一次，前缀缓存可命中。cache/semantic_cache/embed_cache 为
    exact / 语义 / 向量三级缓存实例，None 表示该级关闭。
    """
    system_prompt = BASE_SYSTEM_PROMPT
    if store is not None:
        injection = build_injection(store)
        if injection:
            system_prompt = f"{system_prompt}\n\n{injection}"
    if skills_table:
        system_prompt = f"{system_prompt}\n\n{skills_table}"
    cm = ContextManager(
        ContextConfig(
            system_prompt=system_prompt,
            tools_schema=registry.all_tools(),
        )
    )
    cm.workdir = workdir
    return AgentLoop(
        llm_client=client,
        registry=registry,
        context=cm,
        model=model,
        provider=provider,
        max_steps=MAX_STEPS,
        mode=mode,
        gate=gate,
        telemetry=telemetry,
        cache=cache,
        semantic_cache=semantic_cache,
        embed_cache=embed_cache,
        loaded_skills=loaded_skills,
    )


def _default_store_path() -> str:
    """默认 workspace/memory.db；PHXSC_DB 环境变量优先。"""
    env = os.environ.get("PHXSC_DB")
    if env:
        return env
    path = _project_root() / "workspace" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _default_scheduler_db_path() -> str:
    """默认 scheduler.db 与 memory.db 同一目录。"""
    return str(Path(_default_store_path()).with_name("scheduler.db"))


def _default_embed_cache_path() -> str:
    """默认 embed_cache.db 与 memory.db 同一目录。"""
    return str(Path(_default_store_path()).with_name("embed_cache.db"))


def _default_exact_cache_path() -> str:
    """默认 exact_cache.db 与 memory.db 同一目录。"""
    return str(Path(_default_store_path()).with_name("exact_cache.db"))


def _clear_exact(exact_cache: ExactCache) -> int:
    """清空 exact 两表（cache + cache_meta），返回被清条目数。"""
    return exact_cache.clear()


def _clear_embed(embed_cache: EmbedCache) -> int:
    """清空 embed 的 query_cache 表，返回被清条目数。"""
    return embed_cache.clear()


def _handle_cache(
    session,
    console: Console,
    exact_cache: ExactCache,
    semantic_cache: SemanticCache,
    embed_cache: EmbedCache,
    line: str,
) -> None:
    """/cache stats / clear [semantic|exact|all]：缓存统计与确认式清空。

    stats 一屏表格显示 exact / semantic / embed 三表 entries + hit_rate；
    clear 需输入 y 确认（n 取消）；semantic=清 semantic 两表，exact=清 exact
    两表，all=全清（含 embed）。参数非法打印用法。
    """
    parts = line.split()
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "stats":
        exact_s = exact_cache.stats()
        semantic_s = semantic_cache.stats()
        embed_count = embed_cache.count()
        table = Table(title="缓存统计")
        table.add_column("缓存")
        table.add_column("entries")
        table.add_column("hit_rate")
        table.add_row("exact", str(exact_s["entries"]), f"{exact_s['hit_rate'] * 100:.1f}%")
        table.add_row("semantic", str(semantic_s["entries"]), f"{semantic_s['hit_rate'] * 100:.1f}%")
        table.add_row("embed", str(embed_count), "—")
        console.print(table)
    elif sub == "clear":
        if len(parts) < 3 or parts[2] not in ("semantic", "exact", "all"):
            console.print("[yellow]用法：/cache clear \\[semantic|exact|all][/yellow]")
            return
        target = parts[2]
        label = {"semantic": "语义缓存", "exact": "精确缓存", "all": "全部缓存"}[target]
        answer = _input_line(session, f"确认清空{label}？(y/n) ")
        if answer.strip().lower() != "y":
            console.print("[dim]已取消[/dim]")
            return
        cleared = []
        if target in ("semantic", "all"):
            cleared.append(f"semantic {semantic_cache.clear()} 条")
        if target in ("exact", "all"):
            cleared.append(f"exact {_clear_exact(exact_cache)} 条")
        if target == "all":
            cleared.append(f"embed {_clear_embed(embed_cache)} 条")
        console.print(f"[green]已清空{' / '.join(cleared)}[/green]")
    else:
        console.print("[yellow]用法：/cache stats 或 /cache clear \\[semantic|exact|all][/yellow]")


def _print_help(console: Console) -> None:
    """/help：打印全部斜杠命令清单（rich Table）。"""
    table = Table(title="PhySc-agent 命令")
    table.add_column("命令")
    table.add_column("说明")
    for cmd, desc in (
        ("/plan /investigate /typeset", "切换工作模式"),
        ("/new", "开启新会话（清空对话上下文）"),
        ("/gate <问题>", "本轮引用溯源校验（先检索收集证据再作答，论断须有来源支撑）"),
        ("/moa <问题>", "多助手并行（主控拆解→N 模型并行→聚合；调研类会并行找文献并防重复）"),
        ("/dedup [--file <路径>] [--rewrite] <文本>", "查重检测（--file 检测文件内容；--rewrite 附 AI 降重建议）"),
        ("/schedule list|add|rm", "管理定时任务（list/add <cron> <topic>/rm <id>）"),
        ("/cache stats|clear", "查看缓存统计 / 清空缓存（clear [semantic|exact|all]，需确认）"),
        ("/skill list|loaded|load|unload", "管理技能（list 查看 / loaded 查看注入轨 / load <name> 加载 / unload <name> 卸载）"),
        ("/mcp list|status", "查看 MCP servers 连接状态（phxsc.mcp.json 配置）"),
        ("/sessions", "列出历史会话（按最近更新倒序）"),
        ("/search <词>", "全文检索历史消息（至少 3 个字符）"),
        ("/resume <id>", "恢复历史会话（清空当前上下文后载入，并切到其模式）"),
        ("/fork <id>", "把历史会话消息并入当前上下文（不重置）"),
        ("/model [名称]", "查看当前模型 / 运行时切换模型"),
        ("/provider [名称]", "查看 provider 列表 / 切换 provider（如 /provider zhipu）"),
        ("/stop", "中断当前正在处理的任务"),
        ("/help", "显示本帮助"),
        ("/exit", "退出 CLI"),
        ("Tab / ↑↓", "输入 / 后 Tab 自动补全命令；方向键移动光标 / 浏览历史"),
    ):
        table.add_row(cmd, desc)
    console.print(table)


def _handle_new(loop: AgentLoop, console: Console) -> None:
    """/new：开启新会话——清空对话上下文日志，保留记忆/证据/缓存与闸门状态。

    batch76 #2：prefix 命中 token 字段在常驻 loop 实例上累计（loop.py 只累加
    不清零），新会话必须归零，否则状态栏 cache%（当前会话口径）会带出
    上一会话数据。"""
    loop.context.reset()
    loop.prefix_hit_tokens = 0
    loop.prefix_miss_tokens = 0
    console.print("[green]已开启新会话[/green]")


def _set_thinking(client: "ThinkingLLM", level: ThinkingLevel) -> None:
    """切换档位 + 持久化到 ~/.phxsc/settings.json（下次启动/重开会话保持）。"""
    client.set_level(level)
    try:
        from phxsc.settings import save_thinking_level
        save_thinking_level(level)
    except Exception:
        pass  # 持久化失败不阻断切换
    print(f"🧠 reasoning effort: {level.value}")


def _handle_thinking(client: "ThinkingLLM", line: str) -> None:
    """/thinking [off|low|medium|high]：无参=显示当前档位；on=medium 兼容别名；其余打印用法。"""
    parts = line.split()
    if len(parts) == 1:
        print(f"🧠 reasoning effort: {client.level.value}")
    elif parts[1] == "off":
        _set_thinking(client, ThinkingLevel.OFF)
    elif parts[1] == "low":
        _set_thinking(client, ThinkingLevel.LOW)
    elif parts[1] == "medium":
        _set_thinking(client, ThinkingLevel.MEDIUM)
    elif parts[1] == "high":
        _set_thinking(client, ThinkingLevel.HIGH)
    elif parts[1] == "on":  # 兼容别名 → 推荐默认档
        _set_thinking(client, ThinkingLevel.MEDIUM)
    else:
        print("用法: /thinking [off|low|medium|high]（无参=显示当前档位；on=medium 别名）")


def _handle_voice(loop: "AgentLoop", line: str) -> None:
    """/voice [academic|natural]：无参=显示当前档位；academic/natural=切换；其余打印用法。"""
    parts = line.split()
    if len(parts) == 1:
        print(f"当前 voice: {loop.voice}")
    elif parts[1] == "academic":
        loop.voice = "academic"
        print("🗣 voice: academic")
    elif parts[1] == "natural":
        loop.voice = "natural"
        print("🗣 voice: natural")
    else:
        print("用法: /voice [academic|natural]")


def _handle_model(loop: "AgentLoop", client: "ThinkingLLM", line: str) -> None:
    """/model [名称]：无参=显示当前 provider/模型；有参=切模型。

    - /model <名> 无斜杠 → 当前 provider 内切换（loop.model + settings 保存）
    - /model <provider>/<名> 含斜杠 → 跨 provider 切换（build_client 重建 + settings 保存）
    缓存盐含 provider|model（batch54），切换后旧缓存自动失效。
    """
    parts = line.split()
    if len(parts) == 1:
        print(f"当前模型：{loop.provider}/{loop.model}")
    elif len(parts) == 2 and parts[1].strip():
        target = parts[1].strip()
        if "/" in target:
            pname, _, mname = target.partition("/")
            try:
                raw, pname_ok, mname_ok = build_client(pname, mname or None)
            except ProviderKeyError as exc:
                print(f"错误：{exc}")
                return
            client.set_inner(raw)
            client.set_provider(pname_ok)
            loop.provider = pname_ok
            loop.model = mname_ok
            save_settings({**load_settings(), "provider": pname_ok, "model": mname_ok})
            print(f"模型已切换：{pname_ok}/{mname_ok}")
        else:
            loop.model = target
            save_settings({**load_settings(), "model": target})
            print(f"模型已切换：{loop.provider}/{target}")
    else:
        print("用法：/model [provider/模型名 | 模型名]")


def _handle_provider(loop: "AgentLoop", client: "ThinkingLLM", line: str) -> None:
    """/provider [名称]：无参=列出全部 provider（★=当前）+ 当前模型；有参=切换。

    切换 = build_client 重建底层 client + settings 持久化（重启恢复）。
    """
    parts = line.split()
    if len(parts) == 1:
        providers = all_providers()
        for name, cfg in providers.items():
            mark = "★" if name == loop.provider else " "
            status = cfg.get("status", "")
            default_model = cfg.get("default_model", "")
            print(f"{mark} {name}（{status}，默认模型 {default_model}）")
        print(f"当前：{loop.provider}/{loop.model}")
    elif len(parts) == 2 and parts[1].strip():
        name = parts[1].strip()
        try:
            raw, pname, mname = build_client(name, None)
        except ProviderKeyError as exc:
            print(f"错误：{exc}")
            return
        client.set_inner(raw)
        client.set_provider(pname)
        loop.provider = pname
        loop.model = mname
        save_settings({**load_settings(), "provider": pname, "model": mname})
        print(f"provider 已切换：{pname}/{mname}")
    else:
        print("用法：/provider [名称]")


def _handle_stop(state: "_UIState", loop: "AgentLoop") -> None:
    """/stop：任务执行中设置 interrupt_event（下一轮循环检查生效）；空闲时提示。"""
    if state.busy:
        print("正在中断…")
        loop.interrupt_event.set()
    else:
        print("当前没有正在处理的任务")


def _unknown_command_message(line: str) -> str:
    """未知命令提示文本（含 /help 指引）。"""
    return (
        f"未知命令：{line}（输入 /help 查看全部命令；可用：/exit，"
        f"/{'/ '.join(MODE_NAMES)}，/new，/gate <问题>，/schedule list|add|rm）"
    )


def _gate_question(line: str) -> str | None:
    """/gate 前缀解析（Day 12 前缀化）：/gate <问题> 返回问题文本；无参 / 旧 on|off 返回 None。

    旧版 /gate on|off 全局开关已取消，只剩请求级前缀 /gate <问题>；无参、
    历史用法 on|off 或非 /gate 行均返回 None（调用方打印用法提示）。
    """
    if line == "/gate":
        return None
    if not line.startswith("/gate "):
        return None
    rest = line[len("/gate") :].strip()
    if rest in ("on", "off"):
        return None
    return rest


def _parse_moa(line: str) -> str | None:
    """/moa 前缀解析：/moa <问题> 返回问题文本；非 /moa 行或空问题返回 None。"""
    if line == "/moa":
        return None
    if not line.startswith("/moa "):
        return None
    rest = line[len("/moa") :].strip()
    return rest or None


_MOA_SURVEY_STARTS = ("调研", "检索", "查", "综述")
_MOA_SURVEY_CONTAINS = ("找文献", "文献综述")
_MOA_GEN_CONTAINS = ("PPT", "幻灯片", "文档", "报告", "章节")


def _moa_task_type(question: str) -> str:
    """MoA task_type 判定：调研/检索/查/综述开头或含找文献/文献综述 → survey；
    含 PPT/幻灯片/文档/报告/章节 → generate；否则 qa（survey 优先于 generate）。"""
    if question.startswith(_MOA_SURVEY_STARTS) or any(
        k in question for k in _MOA_SURVEY_CONTAINS
    ):
        return "survey"
    if any(k in question for k in _MOA_GEN_CONTAINS):
        return "generate"
    return "qa"


def _moa_worker_cfgs() -> list[dict]:
    """settings.moa_workers（"provider:model" 列表）→ MoaRunner worker_cfgs（>4 已被截断）。"""
    return [
        {"name": provider, "model": model}
        for entry in load_moa_workers()
        for provider, _, model in [entry.partition(":")]
    ]


def _handle_moa(loop: "AgentLoop", client: "ThinkingLLM", line: str) -> None:
    """/moa <问题>：主控拆解 → N 模型并行（seen 共享防重）→ 聚合输出。

    主控 client 用当前 ThinkingLLM 的底层 client（_inner，不带 thinking 注入），
    model 用当前 loop.model，registry 用当前注册器。执行前打印启动行。
    """
    question = _parse_moa(line)
    if question is None:
        print("用法：/moa <问题>（多助手并行：主控拆解→N 模型并行→聚合；调研类会并行找文献并防重复）")
        return
    worker_cfgs = _moa_worker_cfgs()
    task_type = _moa_task_type(question)
    print(f"MoA 启动：{len(worker_cfgs)} 助手并行（task_type={task_type}）")
    inner = getattr(client, "_inner", client)
    try:
        text = run_moa(inner, loop.model, loop.registry, task_type, question, worker_cfgs)
    except Exception as exc:  # noqa: BLE001
        text = f"MoA 执行失败：{type(exc).__name__}: {exc}"
    print(text)


def _handle_moa_rich(
    loop: "AgentLoop", client: "ThinkingLLM", line: str,
    state: _UIState, session: PromptSession | None, prompt: str,
) -> None:
    """Rich 路径 /moa：worker + 输入轮询，阻塞期间不静默（U4）。

    run_moa 在 worker 线程执行，主线程轮询读输入（与普通问题同模式）：
    /stop 提示 MoA 暂不支持中断，其他输入提示处理中；底部 toolbar 显示
    working+耗时（state.busy/turn_start 驱动）。MoA 不可中途停止（文档保留）。
    """
    question = _parse_moa(line)
    if question is None:
        print("用法：/moa <问题>（多助手并行：主控拆解→N 模型并行→聚合；调研类会并行找文献并防重复）")
        return
    worker_cfgs = _moa_worker_cfgs()
    task_type = _moa_task_type(question)
    print(f"MoA 启动：{len(worker_cfgs)} 助手并行（task_type={task_type}）")
    state.busy = True
    state.turn_start = time.perf_counter()
    try:
        results: list = []

        def _run_moa_in_thread() -> None:
            inner = getattr(client, "_inner", client)
            try:
                results.append(
                    run_moa(inner, loop.model, loop.registry, task_type, question, worker_cfgs)
                )
            except Exception as exc:  # noqa: BLE001
                results.append(f"MoA 执行失败：{type(exc).__name__}: {exc}")

        worker = threading.Thread(target=_run_moa_in_thread, daemon=True)
        worker.start()
        while worker.is_alive():
            try:
                stop_line = _input_line(session, prompt)
            except EOFError:
                break
            s = stop_line.strip()
            if s == "/stop":
                print("MoA 暂不支持中断，请等待完成")
            elif s:
                print("MoA 处理中…")
        worker.join()
        if results:
            print(results[0])
    finally:
        state.last_turn_seconds = time.perf_counter() - state.turn_start
        state.busy = False


def _parse_dedup(line: str) -> dict | None:
    """/dedup 前缀解析：/dedup [--file <路径>] [--rewrite] <文本> → {text, file_path, rewrite}。

    非 /dedup 行、无参数、--file 缺路径、引号未闭合等格式错误均返回 None
    （调用方打印用法）。--file 与 <文本> 同给时以文件内容为准。
    """
    if not line.startswith("/dedup"):
        return None
    rest = line[len("/dedup") :].strip()
    try:
        parts = shlex.split(rest, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    file_path = None
    rewrite = False
    text_tokens = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok == "--file":
            if file_path is not None or i + 1 >= len(parts):
                return None
            file_path = parts[i + 1]
            i += 2
        elif tok == "--rewrite":
            rewrite = True
            i += 1
        else:
            text_tokens.append(tok)
            i += 1
    text = " ".join(text_tokens).strip()
    if file_path is None and not text:
        return None
    return {"text": text, "file_path": file_path, "rewrite": rewrite}


def _dedup_rewrite_once(client, model: str, snippet: str) -> str:
    """单段降重：当前 ThinkingLLM 一次调用（固定 prompt 模板），失败抛给调用方。"""
    resp = client.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": dedup_tools.REWRITE_PROMPT_TEMPLATE.format(snippet=snippet),
            }
        ],
        stream=False,
    )
    return resp.choices[0].message.content or ""


def _handle_dedup(loop, client, console, store, workdir: str, parsed: dict) -> None:
    """/dedup 执行：惰性建库 → detect → Markdown 报告落盘 → 控制台摘要。

    --file 路径过沙箱白名单（越界报错返回）；报告写入
    <workdir>/typeset/dedup_report_<YYYYmmdd_HHMMSS>.md；--rewrite 时对报告
    前 10 个命中片段逐段生成改写建议（失败静默标注"改写失败"）。
    只读原文/原文件，绝不修改。
    """
    text = parsed["text"]
    if parsed["file_path"]:
        try:
            real = safe_read_path(parsed["file_path"], workdir)
        except ValueError as exc:
            console.print(f"[red]错误：{exc}[/red]")
            return
        try:
            text = Path(real).read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]错误：读取文件失败 - {exc}[/red]")
            return
    if not text.strip():
        console.print("[yellow]文本为空，无内容可检测[/yellow]")
        return
    db = dedup_tools.DedupIndex(str(Path(workdir) / "dedup_index.db"))
    try:
        if db.count() == 0:
            console.print("正在建立对照源索引…")
            stats = dedup_tools.build_index(db, Path(workdir) / "papers", store)
            console.print(
                f"[dim]索引完成：文件 {stats['files_indexed']}，"
                f"跳过 {stats['files_skipped']}，shingle {stats['shingles_added']}[/dim]"
            )
        result = dedup_tools.detect(db, text)
    finally:
        db.close()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = Path(workdir) / "typeset"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"dedup_report_{ts}.md"
    lines = [
        f"# 查重检测报告（{ts}）",
        "",
        "## 总览",
        "",
        f"- 总 shingle 数：{result['total_shingles']}",
        f"- 重复 shingle 数：{result['dup_shingles']}",
        f"- 重复率：{result['dup_rate'] * 100:.1f}%",
        "",
        "## 命中明细",
        "",
        "| # | 重复片段 | 出处 source_id | 页码 | 距离 |",
        "| --- | --- | --- | --- | --- |",
    ]
    if result["matches"]:
        for i, m in enumerate(result["matches"], 1):
            snippet = str(m["snippet"]).replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {i} | {snippet} | {m['source_id']} | {m['page']} | {m['distance']} |"
            )
    else:
        lines.append("| （无命中） | | | | |")
    if parsed["rewrite"] and result["matches"]:
        lines.extend(["", "## 改写建议", ""])
        model = getattr(loop, "model", "deepseek-v4-flash")
        for i, m in enumerate(result["matches"][:10], 1):
            snippet = str(m["snippet"])
            lines.append(f"### 片段 {i}")
            lines.append(f"原片段：{snippet}")
            try:
                rewritten = _dedup_rewrite_once(client, model, snippet)
            except Exception:  # noqa: BLE001  降重失败静默，不中断报告生成
                rewritten = ""
            lines.append(f"改写建议：{rewritten or '改写失败'}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]查重报告已生成：{report_path}[/green]")
    console.print(
        f"重复率 {result['dup_rate'] * 100:.1f}%"
        f"（总 shingle {result['total_shingles']}，重复 {result['dup_shingles']}），"
        f"命中 {len(result['matches'])} 条"
    )


def _split_schedule(line: str) -> list[str]:
    """/schedule 命令分词：引号包裹的 cron 作为一个整体（shlex，posix 模式）。

    shlex 是 stdlib，不执行 glob 展开（* 保持字面量），中文/特殊字符原样保留；
    引号未闭合等分词错误返回空列表（上层打印用法）。
    """
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return []


def _schedule_add_args(parts: list[str]) -> tuple[str, str] | None:
    """从 add 子命令分词中取出 (cron, topic)；参数不足或 topic 为空返回 None。

    引号包裹的 cron 是一个 token（含空格，如 "每天 9:00" / "0 9 * * *"）；
    未引号时按标准 5 段 crontab 前截（/schedule add 0 9 * * * perovskite）。
    """
    if len(parts) < 4:
        return None
    first = parts[2]
    if " " in first:
        cron, rest = first, parts[3:]
    else:
        cron, rest = " ".join(parts[2:7]), parts[7:]
    topic = " ".join(rest).strip()
    if not topic:
        return None
    return cron, topic


def _handle_schedule(scheduler_svc, console: Console, line: str) -> None:
    """/schedule list|add <cron> <topic>|rm <id> 命令处理。

    cron 支持标准五段 crontab（"0 9 * * *"）与中文简写（"每天 9:00"），
    其余格式一律按 crontab 解析，非法表达式报错。
    """
    parts = _split_schedule(line)
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        rows = scheduler_svc.list()
        if not rows:
            console.print("[yellow]暂无定时任务[/yellow]")
            return
        table = Table(title="定时任务")
        for col in ("id", "name", "cron", "topic", "enabled", "last_run"):
            table.add_column(col)
        for r in rows:
            table.add_row(
                str(r["id"]),
                r["name"],
                r["cron"],
                r["topic"],
                str(r["enabled"]),
                r["last_run"] or "—",
            )
        console.print(table)
    elif sub == "add":
        parsed = _schedule_add_args(parts)
        if parsed is None:
            console.print(
                '[yellow]用法：/schedule add "每天 9:00" <topic>'
                ' 或 /schedule add "0 9 * * *" <topic>'
                ' 或 /schedule add 0 9 * * * <topic>[/yellow]'
            )
            return
        cron, topic = parsed
        try:
            job_id = scheduler_svc.add(name=topic, cron=cron, topic=topic)
        except ValueError as exc:
            console.print(f"[red]错误：{exc}[/red]")
            return
        console.print(
            f"[green]已添加定时任务 #{job_id}[/green]：cron={cron} topic={topic}"
        )
    elif sub == "rm":
        if len(parts) < 3:
            console.print("[yellow]用法：/schedule rm <id>[/yellow]")
            return
        try:
            job_id = int(parts[2])
        except ValueError:
            console.print("[red]错误：id 必须是数字[/red]")
            return
        if scheduler_svc.remove(job_id):
            console.print(f"[green]已移除定时任务 #{job_id}[/green]")
        else:
            console.print(f"[red]错误：定时任务 #{job_id} 不存在[/red]")
    else:
        console.print("[yellow]用法：/schedule list|add <cron> <topic>|rm <id>[/yellow]")


def _handle_skill(console: Console, metas, loaded_skills: dict[str, str], line: str) -> None:
    """/skill list|loaded|load <name>|unload <name>：技能管理（跨会话，/new 不清空）。"""
    parts = line.split()
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "list":
        if not metas:
            console.print("[yellow]暂无可用技能[/yellow]")
            return
        for m in metas:
            if m.name in loaded_skills:
                console.print(
                    f"- {m.name}: {m.description}（v{m.version}，"
                    f"[green]★ 已加载 {len(loaded_skills[m.name])} 字符[/green]）"
                )
            else:
                console.print(f"- {m.name}: {m.description}（v{m.version}，{m.path}）")
    elif sub == "load":
        if len(parts) < 3:
            console.print("[yellow]用法：/skill load <name>[/yellow]")
            return
        name = parts[2]
        if name not in loaded_skills and len(loaded_skills) >= 8:
            console.print("[yellow]已达技能加载上限（8 个），请先 /skill unload <name>[/yellow]")
            return
        body = load_skill_body(name, metas)
        if body is None:
            console.print(f"[red]技能 {name!r} 不存在（/skill list 查看可用技能）[/red]")
            return
        loaded_skills[name] = body.content
        console.print(f"[green]已加载 {name}（{len(body.content)} 字符）[/green]")
        total = sum(len(c) for c in loaded_skills.values())
        if total > SKILL_WARN_BYTES:
            console.print(
                f"[yellow]⚠️ 注入轨当前总量 {total/1024:.1f}KB（{total} 字符），"
                f"每轮将全文注入；注意 token 开销与注意力稀释，"
                f"不需要常驻的技能可 /skill unload 释放[/yellow]"
            )
    elif sub == "unload":
        if len(parts) < 3:
            console.print("[yellow]用法：/skill unload <name>[/yellow]")
            return
        name = parts[2]
        if name in loaded_skills:
            del loaded_skills[name]
            console.print(f"[green]已卸载 {name}[/green]")
        else:
            console.print(f"[yellow]技能 {name!r} 未加载[/yellow]")
    elif sub == "loaded":
        if not loaded_skills:
            console.print("[yellow]注入轨为空（/skill load <name> 加载技能）[/yellow]")
            return
        total = 0
        for name, content in loaded_skills.items():
            total += len(content)
            console.print(f"- {name}（{len(content)} 字符）")
        console.print(f"[dim]注入轨总量 {total} 字符（约 {total//512} KB），每轮随 user 消息注入[/dim]")
    else:
        console.print("[yellow]用法：/skill list|loaded|load <name>|unload <name>[/yellow]")


def _handle_mcp(console: Console, mcp_registry: McpRegistry | None, line: str) -> None:
    """/mcp list|status：列出已连接 MCP server、工具数与失败原因。

    未配置 phxsc.mcp.json（或全部连接失败）打印提示，不报错。
    """
    parts = line.split()
    if len(parts) > 1 and parts[1] not in ("list", "status"):
        console.print("[yellow]用法：/mcp list 或 /mcp status[/yellow]")
        return
    if mcp_registry is None or not mcp_registry.connected():
        console.print("[yellow]未配置 MCP servers（phxsc.mcp.json）[/yellow]")
        return
    table = Table(title="MCP servers")
    table.add_column("server")
    table.add_column("工具数")
    for name in mcp_registry.connected():
        table.add_row(name, str(mcp_registry.tool_count(name)))
    console.print(table)
    for fail in mcp_registry.failures():
        console.print(f"[yellow]连接失败：{fail}[/yellow]")


def _handle_sessions(console: Console, session_store: SessionStore) -> None:
    """/sessions：列出历史会话（按最近更新倒序；行格式 [id] 时间 · 标题 · N条 · 首条）。"""
    rows = session_store.list_sessions()
    if not rows:
        console.print("[yellow]暂无历史会话[/yellow]")
        return
    for r in rows:
        title = (r.get("title") or "").strip() or "未命名"
        first = (r["first_message"] or "")[:30]
        parts = [
            f"\\[{r['id']}]",
            r["updated_at"][:16],
            title,
            f"{r['message_count']}条",
            first,
        ]
        console.print(" ".join(parts))


def _handle_search(console: Console, session_store: SessionStore, line: str) -> None:
    """/search <词>：trigram 全文检索历史消息（<3 字符返回空提示）。"""
    parts = line.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        console.print("[yellow]用法：/search <词>（至少 3 个字符）[/yellow]")
        return
    hits = session_store.search(parts[1].strip())
    if not hits:
        console.print("[yellow]无匹配（查询至少 3 个字符）[/yellow]")
        return
    for h in hits:
        content = (h["content"] or "")[:60]
        console.print(
            f"\\[{h['session_id']}#{h['seq']}] {h['ts'][:16]} {h['role']} {content}"
        )


def _handle_resume(
    loop: "AgentLoop", console: Console, session_store: SessionStore, line: str
) -> None:
    """/resume <id>：清空当前上下文后载入历史会话并切到其模式。

    逐条 append 用 tool_call_id 规则：tool 必传，其他角色传 None（原样从库中
    读回）；append 抛 ValueError（理论不发生的角色交替违规）捕获打印不崩溃。
    """
    parts = line.split()
    if len(parts) < 2:
        console.print("[yellow]用法：/resume <id>（/sessions 查看 id）[/yellow]")
        return
    sid = parts[1]
    msgs = session_store.load_messages(sid)
    if not msgs:
        console.print(f"[yellow]会话 {sid} 不存在，/sessions 查看列表[/yellow]")
        return
    try:
        loop.context.reset()
        for m in msgs:
            tc = m["tool_call_id"] if m["role"] == "tool" else None
            loop.context.append(m["role"], m["content"], tool_call_id=tc)
        mode = session_store.get_mode(sid) or loop.mode
        loop.mode = mode
        console.print(
            f"[green]已恢复会话 {sid}（{len(msgs)} 条消息，模式 {mode}）[/green]"
        )
    except ValueError as exc:
        console.print(f"[red]错误：{exc}[/red]")


def _handle_fork(
    loop: "AgentLoop", console: Console, session_store: SessionStore, line: str
) -> None:
    """/fork <id>：把历史会话消息并入当前上下文（不重置）。

    当前上下文末尾已是 user 而历史首条也是 user（user→user 违规）时跳过历史
    首条 user，保证并入后角色交替合法；append 抛 ValueError 同样捕获打印。
    """
    parts = line.split()
    if len(parts) < 2:
        console.print("[yellow]用法：/fork <id>（/sessions 查看 id）[/yellow]")
        return
    sid = parts[1]
    msgs = session_store.load_messages(sid)
    if not msgs:
        console.print(f"[yellow]会话 {sid} 不存在，/sessions 查看列表[/yellow]")
        return
    try:
        to_append = list(msgs)
        current = loop.context.build_messages()
        last_role = current[-1]["role"] if len(current) > 1 else None
        if last_role == "user" and to_append and to_append[0]["role"] == "user":
            to_append = to_append[1:]
        for m in to_append:
            tc = m["tool_call_id"] if m["role"] == "tool" else None
            loop.context.append(m["role"], m["content"], tool_call_id=tc)
        console.print(f"[green]已并入会话 {sid} 的 {len(to_append)} 条消息[/green]")
    except ValueError as exc:
        console.print(f"[red]错误：{exc}[/red]")


def _load_dotenv() -> None:
    """从 .env 文件加载环境变量（stdlib 手写解析，不引 python-dotenv）。

    规则：仅当环境变量未设置时才写入（已有环境变量优先，不覆盖）；
    支持 # 注释、空行、KEY=VALUE、KEY="VALUE"（剥引号）。
    .env 路径：<项目根>/.env，可用 PHXSC_ENV_FILE 覆盖。
    """
    env_path = os.environ.get("PHXSC_ENV_FILE") or _default_env_path()
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    global _splash
    parser = argparse.ArgumentParser(prog="phxsc.cli", description="PhySc-agent 交互式 CLI")
    parser.add_argument("--mode", default="investigate", choices=MODE_NAMES)
    parser.add_argument("--workdir", default=None, help="工作目录（默认：workspace/）")
    parser.add_argument("--model", default=None, help=f"LLM 模型（默认：{DEFAULT_MODEL}）")
    parser.add_argument("--provider", default=None, help=f"LLM provider（默认：{DEFAULT_PROVIDER}；内置：deepseek/zhipu/openai/anthropic/kimi/mimo；自定义见 ~/.phxsc/providers.json）")
    parser.add_argument("--no-tui", action="store_true", help="强制使用 Rich CLI（默认 tty 自动启用 TUI）")
    args = parser.parse_args(argv)

    use_tui = not args.no_tui and sys.stdin.isatty() and sys.stdout.isatty()
    bus = EventBus() if use_tui else None

    _load_dotenv()

    workdir = _resolve_workdir(_resolve_workdir_arg(args.workdir))

    # provider 解析：显式 --provider/--model 优先，否则从 settings 恢复（batch55）
    provider_arg = args.provider if args.provider is not None else load_provider()
    model_arg = args.model if args.model is not None else None
    if model_arg is None and load_provider() == provider_arg:
        model_arg = load_model()
    try:
        raw_client, provider_name, model_name = build_client(provider_arg, model_arg)
    except ProviderKeyError as exc:
        if _splash is not None:
            from phxsc.splash import stop_splash
            stop_splash(_splash)
            _splash = None
        print(f"错误：{exc}", file=sys.stderr)
        print(f"可用 provider：{', '.join(all_providers())}（自定义见 ~/.phxsc/providers.json）", file=sys.stderr)
        return 1
    client = ThinkingLLM(raw_client, provider=provider_name)
    # 恢复上次 thinking 档位（~/.phxsc/settings.json；首次启动用默认 high）
    try:
        from phxsc.settings import load_thinking_level
        client.set_level(ThinkingLevel(load_thinking_level()))
    except Exception:
        pass  # 设置异常不阻断启动
    console = Console()
    telemetry = Telemetry()
    summary = telemetry.daily_summary()
    embed_cache = EmbedCache(_default_embed_cache_path())
    semantic_cache = SemanticCache()
    exact_cache = ExactCache(_default_exact_cache_path())
    # 技能元数据：启动扫描一次，只进 system prompt（区1），会话内字节稳定
    skill_metas = scan_skills()
    skills_table = build_metadata_table(skill_metas)
    loaded_skills: dict[str, str] = {}  # cli 层维护，/skill load 写入、loop 只读
    registry = _register_tools(_PrintingRegistry(console, bus), embed_cache, skill_metas)
    # MCP servers：phxsc.mcp.json 配置连接（失败不阻塞启动，打印结果）
    mcp_cfg = load_config()
    mcp_registry: McpRegistry | None = None
    mcp_connected: list[str] = []
    mcp_failed: list[str] = []
    if mcp_cfg.get("servers"):
        mcp_registry = McpRegistry(mcp_cfg, registry)
        failures = mcp_registry.connect_all()
        # 统一两轨（U8）：只收集结果，输出时序由各轨自行决定——
        # TUI 在 app.run() 返回后打印；Rich 在 stop_splash 之后打印（防被 splash 动画吞掉）
        mcp_connected = [
            f"[green]MCP 已连接：{name}（{mcp_registry.tool_count(name)} 工具）[/green]"
            for name in mcp_registry.connected()
        ]
        mcp_failed = [f"[yellow]MCP 连接失败：{fail}[/yellow]" for fail in failures]
    store = MemoryStore(_default_store_path())
    gate = create_gate(client, store, model=model_name)  # CLI 永不 enable：校验由 /gate <问题> 前缀触发
    loop = _build_loop(
        client, registry, args.mode, workdir, model_name,
        provider=provider_name,
        gate=gate, store=store, telemetry=telemetry,
        cache=exact_cache, semantic_cache=semantic_cache, embed_cache=embed_cache,
        skills_table=skills_table, loaded_skills=loaded_skills,
    )
    loop.interrupt_event = threading.Event()
    loop.bus = bus
    session_store = SessionStore(_default_sessions_db_path(workdir))
    if use_tui:
        from types import SimpleNamespace

        from phxsc.ui.app import PhyScApp

        scheduler_svc = create_scheduler(Path(_default_scheduler_db_path()), Path(workdir))
        scheduler_svc.start()
        app = PhyScApp(bus=bus, loop=loop, workdir=workdir)
        app.services = SimpleNamespace(
            console=console, session_store=session_store,
            exact_cache=exact_cache, semantic_cache=semantic_cache, embed_cache=embed_cache,
            scheduler=scheduler_svc, mcp_registry=mcp_registry,
            skill_metas=skill_metas, loaded_skills=loaded_skills,
            client=client, telemetry=telemetry, session=None,
            store=store,
        )
        if _splash is not None:
            from phxsc.splash import stop_splash
            stop_splash(_splash)
            _splash = None
        try:
            app.run()
        finally:
            scheduler_svc.stop()
        for line in mcp_connected:
            console.print(line)
        for line in mcp_failed:
            console.print(line)
        if summary["calls"]:
            console.print(
                f"[dim]今日统计：调用 {summary['calls']} 次 / 总 token {summary['total_tokens']} "
                f"/ 服务端缓存命中率 {summary['prefix_cache_hit_rate'] * 100:.1f}% "
                f"/ 语义缓存命中率 {summary['semantic_hit_rate'] * 100:.1f}% "
                f"/ 估算成本 ${summary['estimated_cost_usd']:.5f}[/dim]"
            )
        else:
            console.print("[dim]今日暂无调用记录[/dim]")
        return 0
    if _splash is not None:
        from phxsc.splash import stop_splash
        stop_splash(_splash)
        _splash = None
    for line in mcp_connected:
        console.print(line)
    for line in mcp_failed:
        console.print(line)
    if summary["calls"]:
        cost_part = (
            "估算成本 未定价"
            if telemetry.pricing_for(loop.model) is None
            else f"估算成本 ${summary['estimated_cost_usd']:.5f}"
        )
        console.print(
            f"[dim]今日统计：调用 {summary['calls']} 次 / 总 token {summary['total_tokens']} "
            f"/ 服务端缓存命中率 {summary['prefix_cache_hit_rate'] * 100:.1f}% "
            f"/ 语义缓存命中率 {summary['semantic_hit_rate'] * 100:.1f}% "
            f"/ {cost_part}[/dim]"
        )
    else:
        console.print("[dim]今日暂无调用记录[/dim]")
    current_session_id = session_store.create_session(loop.mode)
    print(f"PhySc-agent 已启动（模式：{loop.mode}，workdir：{workdir}，模型：{provider_name}/{loop.model}）")
    print(
        "输入 /help 查看命令；/exit 退出；/plan /investigate /typeset 切换模式；"
        "/new 开启新会话；/gate <问题> 触发引用溯源校验；/schedule list|add|rm 定时任务；"
        "/cache stats|clear 缓存统计与管理；/mcp list|status 查看 MCP servers；"
        "/sessions 列出历史会话；/search <词> 检索历史；/resume|/fork <id> 恢复/并入"
    )

    is_tty = sys.stdin.isatty()
    state = _UIState(loop, time.perf_counter())
    session = _build_session(state) if is_tty else None

    scheduler_svc = create_scheduler(Path(_default_scheduler_db_path()), Path(workdir))
    scheduler_svc.start()
    try:
        while True:
            try:
                prompt = f"phxsc[{loop.mode}] > " if is_tty else ""
                line = _input_line(session, prompt)
            except EOFError:
                print()
                return 0
            line = line.strip()
            if not line:
                continue
            gate_round = False
            question = _gate_question(line)
            if question is not None:
                line = question
                gate_round = True
            elif line.startswith("/gate"):
                print("用法：/gate <问题>（在问题前加 /gate，本轮触发引用溯源校验）")
                continue
            dedup_parsed = _parse_dedup(line)
            if dedup_parsed is not None:
                _handle_dedup(loop, client, console, store, workdir, dedup_parsed)
                continue
            elif line.startswith("/dedup"):
                print("用法：/dedup [--file <路径>] [--rewrite] <文本>（--file 检测文件内容；--rewrite 附 AI 降重建议）")
                continue
            if line in ("/exit", "/quit"):
                return 0
            if line == "/help":
                _print_help(console)
                continue
            if line == "/new":
                _handle_new(loop, console)
                current_session_id = session_store.create_session(loop.mode)
                continue
            if line.startswith("/"):
                if line.startswith("/cache"):
                    _handle_cache(session, console, exact_cache, semantic_cache, embed_cache, line)
                    continue
                if line.startswith("/schedule"):
                    _handle_schedule(scheduler_svc, console, line)
                    continue
                if line.startswith("/thinking"):
                    _handle_thinking(client, line)
                    continue
                if line.startswith("/voice"):
                    _handle_voice(loop, line)
                    continue
                if line.startswith("/skill"):
                    _handle_skill(console, skill_metas, loaded_skills, line)
                    continue
                if line.startswith("/mcp"):
                    _handle_mcp(console, mcp_registry, line)
                    continue
                if line.startswith("/sessions"):
                    _handle_sessions(console, session_store)
                    continue
                if line.startswith("/search"):
                    _handle_search(console, session_store, line)
                    continue
                if line.startswith("/resume"):
                    _handle_resume(loop, console, session_store, line)
                    continue
                if line.startswith("/fork"):
                    _handle_fork(loop, console, session_store, line)
                    continue
                if line.startswith("/moa"):
                    _handle_moa_rich(loop, client, line, state, session, prompt)
                    continue
                if line.startswith("/provider"):
                    _handle_provider(loop, client, line)
                    continue
                if line.startswith("/model"):
                    _handle_model(loop, client, line)
                    continue
                if line == "/stop":
                    _handle_stop(state, loop)
                    continue
                mode_name = line[1:]
                if mode_name in MODE_NAMES:
                    loop.mode = mode_name  # 一行切换，上下文保留
                    state.loop = loop
                    print(f"已切换到 {mode_name} 模式")
                    continue
                print(_unknown_command_message(line))
                continue
            state.busy = True
            state.turn_start = time.perf_counter()
            try:
                before = len(loop.context.build_messages())
                if is_tty and loop.interrupt_event is not None:
                    loop.interrupt_event.clear()
                    results: list = []

                    def _run_in_thread() -> None:
                        try:
                            results.append(loop.run(line, gate_round=gate_round))
                        except Exception as exc:  # noqa: BLE001
                            results.append(("__EXC__", exc))

                    worker = threading.Thread(target=_run_in_thread, daemon=True)
                    worker.start()
                    while worker.is_alive():
                        try:
                            stop_line = _input_line(session, prompt)
                        except EOFError:
                            break
                        s = stop_line.strip()
                        if s == "/stop":
                            loop.interrupt_event.set()
                            print("正在中断…")
                            break
                        elif s:
                            print("任务处理中，输入 /stop 中断当前任务")
                    worker.join()
                    if results:
                        r = results[0]
                        if isinstance(r, tuple) and r[0] == "__EXC__":
                            exc = r[1]
                            print(f"错误：{type(exc).__name__}: {exc}")
                            continue
                        answer = r
                    else:
                        answer = "[已中断]"
                else:
                    answer = loop.run(line, gate_round=gate_round)
                if loop.semantic_hit is not None:
                    console.print(
                        f"[dim]⚡ 语义缓存命中 {loop.semantic_hit.score:.2f} "
                        f"← \"{line}\"[/dim]"
                    )
                if loop.cache_hit and loop.semantic_hit is None:
                    console.print("[dim]⚡ 精确缓存命中[/dim]")
                new_msgs = loop.context.build_messages()[before:]
                if new_msgs:
                    session_store.append_round(current_session_id, new_msgs)
            except Exception as exc:
                print(f"错误：{type(exc).__name__}: {exc}")
                continue
            finally:
                state.last_turn_seconds = time.perf_counter() - state.turn_start
                state.busy = False
            print(answer)
    finally:
        scheduler_svc.stop()
        if mcp_registry is not None:
            mcp_registry.close_all()
        embed_cache.close()
        semantic_cache.close()
        exact_cache.close()
        telemetry.close()
        session_store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
