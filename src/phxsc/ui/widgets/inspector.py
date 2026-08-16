"""Inspector v1（batch58）+ contextual（batch59）：右侧研究上下文 / 对象面板。

无选中对象 → RESEARCH CONTEXT（ctx% █░ 进度条 / cache% / session / model /
thinking / voice / gate）。选中 paper → PAPER 面板（文本，同 batch59）；选中
evidence / artifact → 直接显示 batch59 的 EvidenceView / ArtifactView 交互组件
（batch61 挂载：证据流 / 产物分类列表，四态内建）。≥100 列才渲染（app 层
breakpoint 控制 display）。

batch65 挂载（F10）：TaskProgress 组件移到 Inspector 下半区（#inspector-task），
有 task 事件自动显示（update_phase display=True），无任务保持隐藏（DEFAULT_CSS
display:none）；窄高度时 max-height 12 + overflow-y 滚动解决遮挡。

batch73（P1）：上下分栏——上半区内容 #inspector-body 占 1fr，下半区
#inspector-task 占 1fr，中间 #inspector-rule 分界线（Textual 8.x Rule 组件，
无 HorizontalRule）。分界线与 TaskProgress 联动：TaskProgress 收到
update_phase/set_error 转可见时经 _show_hook 通知本类显示分界线；无任务时
两者默认隐藏（DEFAULT_CSS display:none）。

select_object(app, obj)：把对象写入 ui_state.selected_object 并刷新 Inspector——
对象卡片聚焦/点击的选中机制（UI_DESIGN §55/§100）。selected_object 为 UIState
动态扩展字段（batch56 契约未声明，dataclass 允许动态赋值，getattr 兜底）。

batch61 事件接线：evidence_found（普通轮）→ evidence_view.set_success(count)；
artifact_created → artifact_view.add_artifact(path, kind)。两个视图由 app.py 经
本类公开属性/方法访问，事件驱动累积，选中对应对象时展示。
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Rule, Static

from rich.text import Text

from phxsc.ui.theme import TOKENS
from phxsc.ui.state import fmt_tokens
from phxsc.ui.widgets.task_progress import TaskProgress

_PANEL_TITLES = {"paper": "PAPER", "evidence": "EVIDENCE", "artifact": "ARTIFACT"}

_OBJECT_TYPES = ("paper", "evidence", "artifact")


def context_bar(percent: int) -> str:
    """ctx% 进度条：█ 填充 + ░ 空位，10 格。"""
    filled = round(min(100, max(0, percent)) / 10)
    return "█" * filled + "░" * (10 - filled)


def panel_title(obj) -> str:
    """对象 → 面板标题；无选中/未知类型回退 RESEARCH CONTEXT。"""
    if isinstance(obj, dict) and obj.get("type") in _PANEL_TITLES:
        return _PANEL_TITLES[obj["type"]]
    return "RESEARCH CONTEXT"


def select_object(app, obj: dict) -> None:
    """对象卡片聚焦/点击 → UIState.selected_object 更新 → Inspector 重渲染。"""
    try:
        st = getattr(app, "ui_state", None)
    except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
        st = None
    if st is None:
        return
    st.selected_object = obj
    try:
        app.query_one(Inspector).refresh_inspector()
    except Exception:  # noqa: BLE001 Inspector 未挂载（组件独立测试场景）
        pass


def _fmt_relevance(v) -> str | None:
    """relevance → '0.94'；无法格式化返回 None。"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return None


class Inspector(Widget):
    """右侧面板：无选中 RESEARCH CONTEXT / 选中对象动态切换。"""

    DEFAULT_CSS = f"""
    Inspector {{
        color: {TOKENS["text2"]};
    }}
    #inspector-title {{
        text-style: bold;
        color: {TOKENS["text1"]};
        margin-bottom: 1;
    }}
    #inspector-body {{
        height: 1fr;
    }}
    #inspector-rule {{
        display: none;
    }}
    #inspector-task {{
        display: none;
        height: 1fr;
        overflow-y: auto;
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title: Static | None = None
        self._body: Static | None = None
        self._evidence_view = None
        self._artifact_view = None
        self._progress: TaskProgress | None = None
        self._rule: Rule | None = None

    def compose(self) -> ComposeResult:
        # 延迟导入规避循环依赖（evidence_view/artifact_view 反向 import select_object）
        from phxsc.ui.widgets.artifact_view import ArtifactView
        from phxsc.ui.widgets.evidence_view import EvidenceView

        self._title = Static("RESEARCH CONTEXT", id="inspector-title")
        yield self._title
        self._body = Static("", id="inspector-body")
        yield self._body
        self._evidence_view = EvidenceView()
        yield self._evidence_view
        self._artifact_view = ArtifactView()
        yield self._artifact_view
        self._rule = Rule(id="inspector-rule")
        yield self._rule
        self._progress = TaskProgress()
        self._progress.id = "inspector-task"
        self._progress._show_hook = self._sync_task_rule
        yield self._progress

    def _sync_task_rule(self) -> None:
        """TaskProgress 转可见 → 分界线同步显示（batch73 P1 上下分栏）。"""
        if self._rule is not None:
            self._rule.display = True

    def reset_task_panel(self) -> None:
        """task 面板回初始态（/new 或会话切换）：TaskProgress 复位 + 分界线隐藏。"""
        if self._progress is not None:
            self._progress.reset()
        if self._rule is not None:
            self._rule.display = False

    def on_mount(self) -> None:
        self.refresh_inspector()

    @property
    def evidence_view(self):
        assert self._evidence_view is not None
        return self._evidence_view

    @property
    def artifact_view(self):
        assert self._artifact_view is not None
        return self._artifact_view

    @property
    def task_progress(self) -> TaskProgress:
        assert self._progress is not None
        return self._progress

    def set_evidence_success(self, count) -> None:
        """evidence_found（普通轮）→ 证据流 SUCCESS 态。"""
        self.evidence_view.set_success(count)

    def add_artifact(self, path: str = "", kind: str = "note") -> None:
        """artifact_created → 产物分类列表追加。"""
        self.artifact_view.add_artifact(path, kind)

    def refresh_inspector(self) -> None:
        obj = self._selected_object()
        otype = obj.get("type") if isinstance(obj, dict) else None
        if self._title is not None:
            self._title.update(panel_title(obj))
        if otype in ("evidence", "artifact"):
            # 选中 evidence/artifact → 显示对应交互视图，隐藏文本 body
            if self._body is not None:
                self._body.display = False
            if self._evidence_view is not None:
                self._evidence_view.display = otype == "evidence"
            if self._artifact_view is not None:
                self._artifact_view.display = otype == "artifact"
        else:
            if self._body is not None:
                self._body.display = True
                self._body.update(self._render_text())
            if self._evidence_view is not None:
                self._evidence_view.display = False
            if self._artifact_view is not None:
                self._artifact_view.display = False

    def _ui_state(self):
        try:
            return getattr(self.app, "ui_state", None)
        except Exception:  # noqa: BLE001 未挂载时 self.app 抛 NoActiveAppError
            return None

    def _selected_object(self):
        st = self._ui_state()
        if st is None:
            return None
        return getattr(st, "selected_object", None)

    def _render_text(self) -> Text:
        st = self._ui_state()
        if st is None:
            return Text("")
        obj = self._selected_object()
        if isinstance(obj, dict) and obj.get("type") in _OBJECT_TYPES:
            return self._render_object(obj)
        return self._render_context(st)

    def _render_context(self, st) -> Text:
        t = Text()
        t.append("ctx  ", style=TOKENS["text3"])
        t.append(
            f"{fmt_tokens(st.context_used)}/{fmt_tokens(st.context_total)} · "
            f"{context_bar(st.context_percent)} {st.context_percent}%",
            style=TOKENS["text1"],
        )
        if st.prefix_rate is not None:
            t.append("\ncache ", style=TOKENS["text3"])
            t.append(f"{round(st.prefix_rate * 100)}% (服务端)", style=TOKENS["text1"])
        skills = getattr(getattr(self.app, "services", None), "loaded_skills", None)
        t.append("\nskill-inject ", style=TOKENS["text3"])
        if skills:
            total = sum(len(c) for c in skills.values())
            t.append(f"({fmt_tokens(total)}): {', '.join(skills.keys())}", style=TOKENS["text1"])
        else:
            t.append("(0): none", style=TOKENS["text3"])
        t.append("\nsession ", style=TOKENS["text3"])
        session = st.session_id or "new"
        if st.session_title:
            session += f" · {st.session_title}"
        t.append(session, style=TOKENS["text1"])
        t.append("\nmodel ", style=TOKENS["text3"])
        t.append(f"{st.provider}/{st.model or st.provider}", style=TOKENS["text1"])
        t.append("\nthinking ", style=TOKENS["text3"])
        t.append(st.thinking_level or "high", style=TOKENS["text1"])
        t.append("\nvoice ", style=TOKENS["text3"])
        t.append(st.voice or "academic", style=TOKENS["text1"])
        t.append("\ngate ", style=TOKENS["text3"])
        t.append("ON" if st.gate else "OFF", style=TOKENS["warning"] if st.gate else TOKENS["text3"])
        if st.last_error:
            t.append("\nerror ", style=TOKENS["error"])
            t.append(self._trunc(st.last_error, 80) or "", style=TOKENS["error"])
            t.append("\nfix: 检查网络/API key，或 /new 重试", style=TOKENS["text3"])
        return t

    # ---- 对象面板（PAPER / EVIDENCE / ARTIFACT）----

    def _kv(self, t: Text, label: str, value) -> None:
        """label 对齐 12 列的键值行；None/空串跳过。"""
        if value is None or value == "":
            return
        t.append(f"{label:<12}", style=TOKENS["text3"])
        t.append(str(value), style=TOKENS["text1"])
        t.append("\n")

    def _render_object(self, obj: dict) -> Text:
        t = Text()
        otype = obj.get("type")
        if otype == "paper":
            self._render_paper(t, obj)
        elif otype == "evidence":
            self._render_evidence(t, obj)
        else:
            self._render_artifact(t, obj)
        return t

    def _render_paper(self, t: Text, obj: dict) -> None:
        self._kv(t, "Title", obj.get("title"))
        authors = obj.get("authors")
        if isinstance(authors, (list, tuple)):
            authors = ", ".join(str(a) for a in authors)
        self._kv(t, "Authors", authors)
        self._kv(t, "Journal", obj.get("journal"))
        self._kv(t, "Year", obj.get("year"))
        rel = _fmt_relevance(obj.get("relevance"))
        self._kv(t, "Relevance", rel)
        self._kv(t, "Pages", obj.get("pages"))
        self._kv(t, "Evidence", obj.get("evidence"))
        self._kv(t, "Status", obj.get("status"))
        t.append("[r] read [e] evidence [l] lineage", style=TOKENS["text3"])

    def _render_evidence(self, t: Text, obj: dict) -> None:
        self._kv(t, "Source", obj.get("source"))
        self._kv(t, "Page", obj.get("page"))
        self._kv(t, "Snippet", self._trunc(obj.get("snippet"), 60))
        self._kv(t, "Claim", self._trunc(obj.get("claim"), 60))
        t.append("[o] open [c] copy [r] reference", style=TOKENS["text3"])

    def _render_artifact(self, t: Text, obj: dict) -> None:
        self._kv(t, "Type", (obj.get("kind") or "").upper())
        self._kv(t, "Path", obj.get("path"))
        self._kv(t, "Size", obj.get("size"))
        self._kv(t, "Status", obj.get("status"))
        t.append("[o] open [c] copy", style=TOKENS["text3"])

    @staticmethod
    def _trunc(value, n: int) -> str | None:
        if value is None:
            return None
        s = str(value)
        return s if len(s) <= n else s[: n - 1] + "…"

    def text(self) -> str:
        """面板标题 + 正文（测试断言用）。evidence/artifact 走对应视图文本。"""
        obj = self._selected_object()
        title = panel_title(obj)
        otype = obj.get("type") if isinstance(obj, dict) else None
        if otype == "evidence":
            return title + "\n" + self.evidence_view.text()
        if otype == "artifact":
            return title + "\n" + self.artifact_view.text()
        return title + "\n" + str(self._render_text())
