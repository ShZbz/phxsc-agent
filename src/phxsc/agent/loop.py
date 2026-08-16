"""PhySc-agent ReAct 主循环。

AgentLoop 把 LLM 客户端、工具注册器与上下文管理器串成一个 ReAct 循环：
LLM 返回工具调用则执行并回填上下文，返回文本则作为最终答案。内置
thinking 工具 JSON 回收（scavenge）、重复调用抑制（storm）与同工具连续失败中断。
长任务（longtask=True，默认）命中触发启发式时走两阶段 plan-then-execute：
阶段1 用 plan 模式只读工具集产出计划并落 plans/，阶段2 重建上下文全工具执行，
每 PROGRESS_EVERY 步把中间结果摘要写回计划文件（防 Lost in the Middle）。
LLM 客户端按 openai.OpenAI 兼容 duck typing 使用，不引入运行时依赖。
"""

import json
import os
import re
import threading
from collections import deque
from types import SimpleNamespace
import uuid
from datetime import datetime

from phxsc.agent.context import ContextConfig, ContextManager
from phxsc.agent.longtask import (
    PLAN_PROMPT_TEMPLATE,
    append_progress,
    default_workdir,
    is_long_task,
    save_plan,
)
from phxsc.agent.modes import get_mode
from phxsc.providers import get_provider
from phxsc.cache.exact import ExactCache
from phxsc.cache.embed_cache import EmbedCache
from phxsc.cache.semantic import SemanticCache, SemanticHit, is_context_dependent
from phxsc.tools.memory import _get_embedder
from phxsc.ui.events import (
    EVENT_AGENT_CHUNK,
    EVENT_ARTIFACT_CREATED,
    EVENT_CACHE_HIT,
    EVENT_CACHE_MISS,
    EVENT_CONTEXT_USAGE,
    EVENT_GATE_STARTED,
    EVENT_TASK_PHASE_CHANGED,
    EVENT_THINKING_CHUNK,
    EVENT_THINKING_ENDED,
    EVENT_THINKING_STARTED,
)

STORM_WINDOW = 8

# LLM 请求超时（dsh_b2 修复：无 timeout 时 SSE stall 会阻塞 __next__，
# interrupt 检查点不可达 → /stop 无效）。环境变量可覆盖。
LLM_TIMEOUT_ENV = "PHXSC_LLM_TIMEOUT"  # 非流式整次请求总时长
LLM_STREAM_TIMEOUT_ENV = "PHXSC_LLM_STREAM_TIMEOUT"  # 流式 chunk 间 stall 超时
DEFAULT_LLM_TIMEOUT = 300.0
DEFAULT_LLM_STREAM_TIMEOUT = 60.0


def _env_float(name: str, default: float) -> float:
    """环境变量浮点配置；未设置或非法回退 default。"""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default

# 长任务两阶段常量
PLAN_MAX_STEPS = 3  # 阶段1 只读规划最多跑 3 轮
PROGRESS_EVERY = 3  # 阶段2 每 3 步写回一次进度
LONG_TASK_PROGRESS_MARKER = "\n\n执行进度已记录："
PROGRESS_SNIPPET_LEN = 80  # 进度摘要单条工具结果截断长度
PROGRESS_SNIPPET_ROUNDS = 3  # 进度摘要取最近几轮工具结果

STORM_SUPPRESSED = "该调用与最近调用重复，已抑制；请换一种方式或告知用户无法继续"

# 阶段1 执行摘要（P0 失忆修复）
PLAN_SUMMARY_SNIPPET_LEN = 80  # 单条工具结果截断长度
PLAN_SUMMARY_MAX_TOOLS = 5  # 最多保留的工具结果条数

# 计划步骤名解析（batch73 P1：长任务阶段1 plan_text → 步骤名列表；
# batch77：中文序号/表格行/加粗 + 「后续动作」节兜底 + 尾部工具名清洗）
PLAN_STEP_MAX = 20  # 最多提取步骤条数
PLAN_STEP_NAME_LEN = 60  # 单条步骤名截断长度
_PLAN_STEP_NUM_RE = re.compile(r"^\*{0,2}\d+\s*[.．、)）]\s*(.+)$")
_PLAN_STEP_CN_NUM_RE = re.compile(r"^\*{0,2}第[一二三四五六七八九十百\d]+\s*步[：:、.．\s]*(.+)$")
_PLAN_STEP_CIRCLE_RE = re.compile(r"^\*{0,2}[①-⑳]\s*(.+)$")
_PLAN_STEP_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_PLAN_TRAIL_PAREN_RE = re.compile(r"[（(][^（()）]*[）)]\s*$")
_PLAN_TRAIL_TOOL_COLON_RE = re.compile(r"[:：]\s*[^:：]*(?:工具|tool|调用|使用|通过)[^:：]*$")
_PLAN_SECTION_HEADER_RE = re.compile(r"^#{1,6}\s*后续动作\s*$")
_PLAN_HEADER_RE = re.compile(r"^#{1,6}\s+\S")


def _clean_step_name(name: str) -> str:
    """步骤名清洗（batch77）：去掉尾部工具名提示（如「（arxiv_search）」
    「（用 xxx 工具）」「：使用 xxx 工具」）；清洗后为空保留原文。"""
    cleaned = name.strip().strip("*_` ").strip()
    for pat in (_PLAN_TRAIL_PAREN_RE, _PLAN_TRAIL_TOOL_COLON_RE):
        m = pat.search(cleaned)
        if m is not None:
            candidate = cleaned[: m.start()].strip()
            if candidate:
                cleaned = candidate
            break
    return cleaned


def _extract_followup_section(plan_text: str) -> str:
    """提取「## 后续动作」节正文（batch77 兜底）：该标题到下一 `#` 标题之间的行。"""
    lines = plan_text.splitlines()
    start = None
    for i, raw in enumerate(lines):
        if _PLAN_SECTION_HEADER_RE.match(raw.strip()):
            start = i + 1
            break
    if start is None:
        return ""
    body = []
    for raw in lines[start:]:
        if _PLAN_HEADER_RE.match(raw.strip()):
            break
        body.append(raw)
    return "\n".join(body)


def _scan_plan_steps(text: str) -> list[str]:
    """按行扫描步骤名（batch77 格式统一匹配入口）。"""
    steps: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        name = None
        m = _PLAN_STEP_NUM_RE.match(line)
        if m is not None:
            name = m.group(1)
        else:
            m = _PLAN_STEP_CN_NUM_RE.match(line)
            if m is not None:
                name = m.group(1)
            else:
                m = _PLAN_STEP_CIRCLE_RE.match(line)
                if m is not None:
                    name = m.group(1)
                elif line.startswith("|") and line.endswith("|"):
                    # markdown 表格行：| 1 | 任务名 | ... | → 第二列
                    cells = [c.strip() for c in line.strip("|").split("|")]
                    if len(cells) >= 2 and re.fullmatch(r"\d+", cells[0]) and cells[1]:
                        name = cells[1]
                else:
                    m = _PLAN_STEP_BULLET_RE.match(line)
                    if m is not None:
                        name = m.group(1)
        if name is None:
            continue
        cleaned = _clean_step_name(name)
        if not cleaned:
            continue
        steps.append(cleaned[:PLAN_STEP_NAME_LEN])
        if len(steps) >= PLAN_STEP_MAX:
            break
    return steps


def _parse_plan_steps(plan_text: str) -> list[str]:
    """从阶段1 plan_text 提取步骤名称列表（batch73 P1，batch77 加强）。

    按行扫描 `1. xxx` / `- xxx` / `* xxx` / `**1. xxx**` / 中文序号
    （第一步/第1步/①/1、/1．）/ markdown 表格行，去掉序号与 markdown 符号、
    尾部工具名提示后作为步骤名；单条截断 PLAN_STEP_NAME_LEN 字符、
    最多取 PLAN_STEP_MAX 条。行首匹配失败时从「## 后续动作」节兜底提取。
    解析失败/非长任务返回空列表（不破坏现有行为）。
    """
    if not plan_text:
        return []
    steps = _scan_plan_steps(plan_text)
    if not steps:
        section = _extract_followup_section(plan_text)
        if section:
            steps = _scan_plan_steps(section)
    return steps


def _extract_json_blocks(text: str) -> list[str]:
    """提取文本中所有花括号平衡包裹的 JSON 候选块（字符串内的 {} 不计数）。"""
    blocks = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        while j < n:
            ch = text[j]
            if in_str:
                if ch == "\\":
                    j += 2  # 跳过转义字符（含 \"）
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[i : j + 1])
                        break
            j += 1
        if depth == 0:
            i = j + 1
        else:
            i += 1
    return blocks


def _classify_delta(delta) -> tuple:
    """流式 delta 分类。返回 (kind, payload) 或 (None, None)。

    kind: "reasoning" | "content" | "tool_call" | None
    """
    rc = getattr(delta, "reasoning_content", None)
    if rc:
        return ("reasoning", rc)
    content = getattr(delta, "content", None)
    if content:
        return ("content", content)
    tcs = getattr(delta, "tool_calls", None)
    if tcs:
        return ("tool_call", tcs)
    return (None, None)


class AgentLoop:
    """ReAct 主循环：工具调用与最终回答的统一驱动。"""

    def __init__(
        self,
        llm_client,
        registry,
        context,
        model: str = "deepseek-v4-flash",
        provider: str = "deepseek",
        max_steps: int = 15,
        mode: str = "investigate",
        cache: ExactCache | None = None,
        gate: object | None = None,
        telemetry: object | None = None,
        longtask: bool = True,
        voice: str = "academic",
        loaded_skills: dict[str, str] | None = None,
        semantic_cache: SemanticCache | None = None,
        embed_cache: EmbedCache | None = None,
        interrupt_event: threading.Event | None = None,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT,
        llm_stream_timeout: float = DEFAULT_LLM_STREAM_TIMEOUT,
    ) -> None:
        self.llm_client = llm_client
        self.registry = registry
        self.context = context
        self.model = model
        self.provider = provider
        self.max_steps = max_steps
        self.mode = mode
        self.cache = cache
        self.gate = gate
        self.telemetry = telemetry
        self.longtask = longtask
        self.voice = voice
        self.loaded_skills: dict[str, str] = loaded_skills if loaded_skills is not None else {}
        self.semantic_cache = semantic_cache
        self.embed_cache = embed_cache
        self.interrupt_event = interrupt_event
        self.llm_timeout = _env_float(LLM_TIMEOUT_ENV, llm_timeout)
        self.llm_stream_timeout = _env_float(LLM_STREAM_TIMEOUT_ENV, llm_stream_timeout)
        self._run_mark: int = 0
        self._stage2_mark: int | None = None
        self._plan_phase: bool = False
        self.bus = None  # UI 事件总线：cli 层构建后属性注入（同 interrupt_event 先例）；None=无 TUI
        self.cache_hit: bool = False
        self.semantic_hit: SemanticHit | None = None
        self.semantic_misses: int = 0
        self.prefix_hit_tokens: int = 0
        self.prefix_miss_tokens: int = 0
        self.last_usage: dict = {}
        self.total_tokens: int = 0
        self.last_steps: int = 0
        self._storm = deque(maxlen=STORM_WINDOW)
        self._compressor = None
        self._consecutive_failures = 0
        self._last_error_tool: str | None = None
        self.last_reasoning: str | None = None

    def run(self, user_input: str, gate_round: bool = False) -> str:
        """执行一次 ReAct 会话，返回最终回答文本。

        gate_round=True 时本轮为引用溯源校验轮（Day 12 请求级 /gate 前缀触发）：
        user 首行注入 [gate: strict] 行为指令、exact cache key 加 "|gate" 盐隔离
        （普通轮未校验缓存不参与）、最终回答强制过 verify(force=True)。其他轮
        一律不校验（防 token 爆炸）。
        命中长任务启发式时走两阶段 plan-then-execute（longtask 开关与
        PHXSC_LONGTASK=0 均可禁用）。exact cache 的 key 按原始 user_input，
        阶段1/阶段2 的 LLM 调用不再重复查 key，避免阶段2 重建上下文误命中。
        最终回答写回 context（assistant 消息），保证 user→assistant→user 交替
        合法——纯文本回答（无工具调用）后不写回会导致下一轮 user→user 违规。
        语义缓存（semantic_cache）在 exact miss 之后、非 gate 轮且非上下文依赖
        query 时查询：命中直接返回缓存 answer（LLM 零调用），未命中继续原流程；
        最终回答在出口写回 semantic store（embedding 复用 lookup 阶段回填的向量）。
        """
        if gate_round and self.bus is not None:
            self.bus.publish(EVENT_GATE_STARTED, question=user_input)
        self.semantic_hit = None
        self._trim_context()
        self._storm.clear()
        cache_mode = self.mode
        if self.mode != "typeset" and self.voice == "natural":
            cache_mode = f"{self.mode}:natural"
        key = None
        if self.cache is not None:
            key = ExactCache.key_for(
                user_input, cache_mode, salt=self._cache_salt(gate_round=gate_round)
            )
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hit = True
                if self.bus is not None:
                    self.bus.publish(EVENT_CACHE_HIT, payload={"kind": "exact", "score": None})
                self.last_steps = 0
                self._record_telemetry(None, 1, True)
                self.context.append(
                    "user", self._decorate_user(user_input, gate_round=gate_round)
                )
                self.context.append("assistant", cached)
                return cached
        self.cache_hit = False
        if (
            self.semantic_cache is not None
            and not gate_round
            and not is_context_dependent(user_input)
        ):
            # embedding 后端不可用（无 key/断网）→ 降级：embedder=None，lookup 走
            # embed_cache 或防御性返回 None（记 miss、旁路语义缓存走 LLM，可选功能不阻断）
            embedder = None
            try:
                embedder = _get_embedder()
            except Exception:  # noqa: BLE001
                pass
            sh = self.semantic_cache.lookup(
                user_input,
                cache_mode.split(":")[0],
                self.voice,
                embedder=embedder,
                embed_cache=self.embed_cache,
            )
            if sh is not None:
                self.semantic_hit = sh
                self.cache_hit = True
                if self.bus is not None:
                    self.bus.publish(EVENT_CACHE_HIT, payload={"kind": "semantic", "score": sh.score})
                self.last_steps = 0
                self._record_telemetry(None, 1, True, semantic_cache_hit=True)
                self.context.append(
                    "user", self._decorate_user(user_input, gate_round=gate_round)
                )
                self.context.append("assistant", sh.answer)
                return sh.answer
            self.semantic_misses += 1
            if self.bus is not None:
                self.bus.publish(EVENT_CACHE_MISS, payload={"kind": "semantic"})
            self._record_telemetry(None, 1, False, semantic_cache_miss=True)
        mark = self.context.checkpoint()
        self._run_mark = mark
        self._stage2_mark = None
        try:
            self.context.append(
                "user", self._decorate_user(user_input, gate_round=gate_round)
            )
            if self.longtask and is_long_task(user_input):
                result = self._run_longtask(user_input, key, gate_round=gate_round)
            else:
                result = self._run_steps(
                    self.max_steps, cache_key=key, gate_round=gate_round
                )
            interrupted = self.interrupt_event is not None and self.interrupt_event.is_set()
            if (
                self.semantic_cache is not None
                and not gate_round
                and not is_context_dependent(user_input)
                and result
                and result.strip()
                and not interrupted
            ):
                vec = self.embed_cache.get(user_input) if self.embed_cache is not None else None
                if vec is not None:
                    store_result = result
                    if LONG_TASK_PROGRESS_MARKER in store_result:
                        store_result = store_result.split(LONG_TASK_PROGRESS_MARKER)[0]
                    try:
                        self.semantic_cache.store(
                            user_input,
                            store_result,
                            cache_mode.split(":")[0],
                            self.voice,
                            vec,
                        )
                    except Exception:  # noqa: BLE001  store 是旁路，失败不阻塞主流程
                        pass
            if result and result.strip() and not interrupted:
                self.context.append("assistant", result, reasoning_content=self.last_reasoning)
            return result
        except Exception:
            self.context.rollback(mark)
            raise

    def _decorate_user(self, text: str, gate_round: bool = False) -> str:
        """每轮 user 首行注入 [mode: xxx] + [gate: strict]（校验轮） + 条件注入
        [voice: natural] + 已加载技能。

        缓存经济学铁律：本段走 user 消息（区2），不进 system prompt（区1）；
        gate 轮指令同样走 user 消息（不走 system prompt 保前缀缓存）；
        loaded_skills 为空时前缀字节不变（零开销，服务端前缀缓存不破）。
        注入轨全文注入不截断，总量控制由 /skill load 时警告承担。
        loaded_skills 是 cli 层维护的 dict 引用，本方法只读，不修改。
        """
        prefix = f"[mode: {self.mode}]"
        if gate_round:
            prefix += "\n[gate: strict] 先检索收集证据再作答，所有论断必须有来源支撑"
        if self.mode == "typeset" or self.voice == "natural":
            prefix += (
                "\n[voice: natural] 面向人读：口语化、句子短、少用"
                "\"首先/其次/最后\"结构词和\"重要/关键/深入\"等堆砌词；"
                "不写\"希望对你有帮助/有问题随时问\"类客套；不编造事实；"
                "输出前自检：这句话像不像 AI 写的？像就改。"
            )
        if self.loaded_skills:
            injected = "\n".join(
                f"### {name}\n{content}"
                for name, content in self.loaded_skills.items()
            )
            prefix += "\n[skills]\n" + injected
        return f"{prefix}\n{text}"

    def _cache_salt(self, gate_round: bool = False) -> str:
        """exact cache key 盐：provider + 模型名 + 已加载 skill 列表（换 provider/模型/load skill 后旧缓存自动失效）。

        gate_round=True 时追加 "|gate"：校验轮与普通轮缓存隔离（防普通轮
        未校验缓存绕过引用溯源校验）。
        """
        base = f"{self.provider}|{self.model}|{sorted(self.loaded_skills.keys())}"
        if gate_round:
            base += "|gate"
        return base

    def _trim_context(self) -> None:
        """每轮上下文裁剪：超 max_window 时压缩最旧轮，避免无界增长。

        裁剪是纯内存操作零成本；LLM 摘要压缩只在实际裁剪发生后触发。
        压缩失败降级为占位符保留，不阻塞主流程，下次再试。
        """
        removed = self.context.trim_window()
        if removed:
            if self._compressor is None:
                self._compressor = self._make_compressor()
            if self._compressor is not None:
                self.context.set_compressor(self._compressor)
            try:
                self.context.compress()
            except Exception:  # noqa: BLE001  压缩失败不阻塞
                self._compressor = None

    def _make_compressor(self):
        """构造压缩回调（LLM 生成中文摘要，≤300 字）。"""

        def _compress(old_messages: list[dict]) -> str:
            formatted = "\n".join(
                f"{m.get('role')}: {m.get('content') or m.get('tool_call_id') or ''}"
                for m in old_messages
            )
            prompt = (
                "把以下对话历史压缩为一段中文摘要（保留关键事实、结论、工具调用"
                "结果、用户偏好；不超过 300 字）：\n" + formatted
            )
            resp = self.llm_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                timeout=self.llm_timeout,
            )
            return resp.choices[0].message.content or "<摘要生成失败>"

        return _compress

    def _run_steps(
        self,
        max_steps: int,
        cache_key: str | None = None,
        plan_path: str | None = None,
        gate_round: bool = False,
        steps: list[str] | None = None,
    ) -> str:
        """标准 ReAct 循环：工具调用 / 最终回答 / 最大步骤限制。

        plan_path 非空时每 PROGRESS_EVERY 步把中间结果摘要追加写回计划文件。
        steps（batch73 P1）为阶段1 plan_text 解析出的步骤名列表，仅在阶段2
        step==1 的首条 task_phase_changed 事件中携带，后续步骤不带。
        最终回答在返回前写入 exact cache（cache_key 非空时，存校验前原文），
        与单阶段行为一致；gate_round=True 时最终回答强制过 verify(force=True)。
        """
        for step in range(1, max_steps + 1):
            interrupted = self._interrupt_return(step)
            if interrupted is not None:
                return interrupted
            self.last_steps = step
            if plan_path is not None and self.bus is not None:
                if step == 1 and steps:
                    self.bus.publish(
                        EVENT_TASK_PHASE_CHANGED,
                        phase="investigate",
                        step=step,
                        total=max_steps,
                        label="执行研究计划",
                        steps=steps,
                    )
                else:
                    self.bus.publish(
                        EVENT_TASK_PHASE_CHANGED,
                        phase="investigate", step=step, total=max_steps, label="执行研究计划",
                    )
            if self.bus is not None:
                self.bus.publish(EVENT_THINKING_STARTED, level=str(getattr(getattr(self.llm_client, "level", None), "value", "") or ""))
            if self.bus is not None and self.provider == "deepseek":
                resp, message = self._stream_call()
            else:
                resp = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=self.context.build_messages(),
                    tools=self.registry.all_tools(),
                    stream=False,
                    timeout=self.llm_timeout,
                )
                self._record_usage(resp)
                message = resp.choices[0].message
                # plan 阶段（只读工具集、≤3 步、弃用上下文）跳过此检查点：
                # 保留阶段1 工具痕迹给 _plan_exec_summary（batch72 P0），
                # 中断由下一步步首 / 每工具后检查点与 _run_longtask 收口
                if not self._plan_phase:
                    interrupted = self._interrupt_return(step)
                    if interrupted is not None:
                        return interrupted
            if self.bus is not None:
                self.bus.publish(EVENT_THINKING_ENDED, level=str(getattr(getattr(self.llm_client, "level", None), "value", "") or ""))
            self._record_telemetry(resp, step, False)
            self.last_reasoning = getattr(message, "reasoning_content", None)
            finish_reason = resp.choices[0].finish_reason
            tool_calls = self._normalize_tool_calls(message)
            if finish_reason != "tool_calls":
                tool_calls.extend(self._scavenge_tool_calls(message))
            if not tool_calls:
                result = message.content
                if result is None or (isinstance(result, str) and not result.strip()):
                    if self.interrupt_event is not None and self.interrupt_event.is_set():
                        return f"[已中断] 任务被用户终止（第 {step} 步）"
                    return f"[空响应] 模型返回空内容（第 {step} 步），请重试或切换模型"
                if cache_key is not None:
                    self.cache.set(cache_key, result)
                if self.gate is not None and gate_round:
                    ok, issues = self.gate.verify(result, force=True)
                    if not ok:
                        result = (
                            result
                            + "\n\n⚠️ [溯源闸门] 以下论断未通过引用验证，请谨慎使用：\n- "
                            + "\n- ".join(issues[:5])
                        )
                return result
            self._append_assistant(tool_calls, reasoning_content=self.last_reasoning)
            for tc in tool_calls:
                if self.bus is not None:
                    self.bus.publish(
                        EVENT_TASK_PHASE_CHANGED,
                        phase="investigate",
                        step=step,
                        total=max_steps,
                        label=tc["function"]["name"],
                    )
                interrupted = self._execute_tool_call(tc)
                if interrupted:
                    return interrupted
                interrupted = self._interrupt_return(step)
                if interrupted is not None:
                    return interrupted
            if plan_path is not None and step % PROGRESS_EVERY == 0:
                append_progress(plan_path, self._step_progress_summary(step))
        return f"达到最大步骤限制（{max_steps}），任务未完成"

    def _interrupt_return(self, step: int) -> str | None:
        """interrupt_event 已置位时按步首语义返回中断说明文本；未置位返回 None。

        与 _run_steps 步首检查共用（plan 阶段 / 阶段2 / 单阶段三种回滚语义），
        供非流式 create 返回后与每个工具执行后的中断检查点复用（dsh_b2）。
        """
        if self.interrupt_event is None or not self.interrupt_event.is_set():
            return None
        self.last_steps = step - 1 if step > 1 else 0
        if self._plan_phase:
            self.context.rollback(self._run_mark)
            return "[已中断] 计划阶段被用户终止"
        if self._stage2_mark is not None:
            summary = self._step_progress_summary(step)
            self.context.rollback(self._stage2_mark)
            msg = f"[已中断] 任务被用户终止（第 {step} 步）"
            if summary:
                msg += "\n" + summary
            self.context.append("assistant", msg)
            return msg
        self.context.rollback(self._run_mark)
        return f"[已中断] 任务被用户终止（第 {step} 步）"

    def _stream_call(self):
        """流式调用（仅 deepseek + bus 存在时进入）。

        返回 (usage_holder, message)。message 为 SimpleNamespace：
        content / reasoning_content / tool_calls / finish_reason 四属性齐全，
        与 openai 响应 message 形状兼容，供下游原逻辑直接消费。
        usage_holder 附带 choices 镜像（finish_reason），保证下游
        `resp.choices[0].finish_reason` 读取路径在流式分支不崩。
        """
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage_holder = None
        finish_reason = "stop"
        interrupted = False
        try:
            stream = self.llm_client.chat.completions.create(
                model=self.model,
                messages=self.context.build_messages(),
                tools=self.registry.all_tools(),
                stream=True,
                stream_options={"include_usage": True},
                timeout=self.llm_stream_timeout,
            )
            for chunk in stream:
                if self.interrupt_event is not None and self.interrupt_event.is_set():
                    interrupted = True
                    break
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    usage_holder = SimpleNamespace(usage=usage)
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    finish_reason = fr
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                kind, payload = _classify_delta(delta)
                if kind == "reasoning":
                    reasoning_parts.append(payload)
                    if self.bus is not None:
                        self.bus.publish(EVENT_THINKING_CHUNK, text=payload)
                elif kind == "content":
                    content_parts.append(payload)
                    if self.bus is not None:
                        self.bus.publish(EVENT_AGENT_CHUNK, text=payload)
                elif kind == "tool_call":
                    for tc in payload:
                        idx = getattr(tc, "index", 0)
                        entry = tool_acc.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        tc_id = getattr(tc, "id", None)
                        if tc_id and not entry["id"]:
                            entry["id"] = tc_id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            name = getattr(fn, "name", None)
                            args = getattr(fn, "arguments", None)
                            if name and not entry["function"]["name"]:
                                entry["function"]["name"] = name
                            if args:
                                entry["function"]["arguments"] += args
        except Exception:
            # 中断优先（dsh_b2）：/stop 已置位时不再回退非流式，
            # 走尾部空 message 让 _run_steps 空内容分支返回中断语。
            if self.interrupt_event is not None and self.interrupt_event.is_set():
                interrupted = True
            else:
                resp = self.llm_client.chat.completions.create(
                    model=self.model,
                    messages=self.context.build_messages(),
                    tools=self.registry.all_tools(),
                    stream=False,
                    timeout=self.llm_timeout,
                )
                self._record_usage(resp)
                return resp, resp.choices[0].message
        if usage_holder is None:
            usage_holder = SimpleNamespace(usage=None)
        usage_holder.choices = [SimpleNamespace(finish_reason=finish_reason)]
        self._record_usage(usage_holder)
        tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
        message = SimpleNamespace(
            content="".join(content_parts) if not interrupted else None,
            reasoning_content="".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )
        return usage_holder, message

    def stats(self) -> dict:
        """本轮只读统计：模式 / 模型 / 步数 / 累计 token / 缓存命中 / 最近用量。

        cache_hit 是本地 exact cache 口径（几乎恒 False）；prefix_hit_rate 是
        DeepSeek 服务端 prefix 缓存口径（累计 prompt_cache_hit/miss_tokens）。
        """
        denom = self.prefix_hit_tokens + self.prefix_miss_tokens
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "steps": self.last_steps,
            "total_tokens": self.total_tokens,
            "cache_hit": self.cache_hit,
            "last_usage": dict(self.last_usage),
            "prefix_hit_tokens": self.prefix_hit_tokens,
            "prefix_miss_tokens": self.prefix_miss_tokens,
            "prefix_hit_rate": self.prefix_hit_tokens / denom if denom else 0.0,
        }

    def _run_longtask(self, user_input: str, cache_key: str | None, gate_round: bool = False) -> str:
        """长任务两阶段：只读规划 → 重建上下文全工具执行，进度写回 plans/。

        阶段1 用独立的 plan 模式 loop（plan 工具集，只读）产出计划文本，落
        <workdir>/plans/<时间戳>-<标题>.md；阶段2 rollback 到本轮前（保留历史
        会话）后以「计划 + 原任务」作为首条 user 消息，用当前模式完整工具集
        执行。任何一步失败都回退到本轮前状态（历史保留），异常后不失忆。
        中断时不 rollback 主 context：保留本轮 user 输入并追加
        assistant(中断说明 + 阶段1执行摘要)，下一轮追问仍有执行痕迹（P0 失忆修复）。
        """
        workdir = getattr(self.context, "workdir", None) or default_workdir()
        try:
            plan_loop = self._build_plan_loop()
            plan_text = plan_loop.run(
                PLAN_PROMPT_TEMPLATE.format(user_input=user_input)
            )
            plan_steps = _parse_plan_steps(plan_text)
            if len(plan_steps) < 3:
                retry_text = plan_loop.run(
                    "步骤清单不符合要求（少于 3 条）。请重新输出完整清单：3-10 条，"
                    "每行 `1. 任务名` 格式，只输出清单不要开始执行。"
                )
                retry_steps = _parse_plan_steps(retry_text)
                if len(retry_steps) >= 3:
                    plan_steps = retry_steps
                    plan_text = retry_text
            plan_summary = self._plan_exec_summary(plan_loop)
            if self.interrupt_event is not None and self.interrupt_event.is_set():
                interrupted_msg = "[已中断] 计划阶段被用户终止"
                if plan_summary:
                    interrupted_msg += f"\n{plan_summary}"
                self.context.append("assistant", interrupted_msg)
                return interrupted_msg
            plan_rel = save_plan(workdir, user_input, plan_text)
            if self.bus is not None:
                self.bus.publish(EVENT_ARTIFACT_CREATED, payload={"path": plan_rel, "kind": "plan"})
                self.bus.publish(
                    EVENT_TASK_PHASE_CHANGED,
                    phase="plan", step=1, total=2, label="研究计划已生成",
                    steps=plan_steps,
                )
            self.context.rollback(self._run_mark)
            phase2_message = (
                f"【任务】请立即按以下计划执行，完成原始任务。\n"
                f"不要复述或确认计划本身，直接调用工具逐步执行，产出最终成果。\n\n"
                f"【执行计划】\n{plan_text}\n\n【原始任务】\n{user_input}"
            )
            if plan_summary:
                phase2_message += f"\n\n{plan_summary}"
            self.context.append(
                "user",
                self._decorate_user(phase2_message, gate_round=gate_round),
            )
            self._stage2_mark = self.context.checkpoint()
            result = self._run_steps(
                self.max_steps,
                cache_key=cache_key,
                plan_path=os.path.join(workdir, plan_rel),
                gate_round=gate_round,
                steps=plan_steps,
            )
        except Exception:
            self.context.rollback(self._run_mark)
            raise
        return f"{result}{LONG_TASK_PROGRESS_MARKER}{plan_rel}"

    def _build_plan_loop(self) -> "AgentLoop":
        """阶段1 只读规划 loop：plan 模式系统提示 + 全量工具 schema。

        只读由权限强制：工具 schema 全量（all_tools），plan 模式调用写工具时
        被 can_call 拒绝，返回 mode_permission 结构化错误。
        """
        plan_cm = ContextManager(
            ContextConfig(
                system_prompt=get_mode("plan").system_prompt,
                tools_schema=self.registry.all_tools(),
            )
        )
        plan_cm.workdir = getattr(self.context, "workdir", None)
        plan_loop = AgentLoop(
            llm_client=self.llm_client,
            registry=self.registry,
            context=plan_cm,
            model=self.model,
            max_steps=PLAN_MAX_STEPS,
            mode="plan",
            gate=self.gate,
            telemetry=self.telemetry,
            longtask=False,
            voice=self.voice,
            interrupt_event=self.interrupt_event,
        )
        plan_loop._plan_phase = True
        plan_loop.bus = self.bus
        return plan_loop

    def _plan_exec_summary(self, plan_loop: "AgentLoop") -> str:
        """从阶段1 plan loop 上下文提取工具执行摘要（P0 失忆修复）。

        遍历 plan_loop.context 的 tool 消息：每条 content 截断 80 字符、
        最多取 5 条，逐行拼接为 f"【阶段1已执行】\\n<每条一行>"；
        无 tool 消息返回空串。plan_loop 上下文独立于主 context，
        须在 rollback(self._run_mark) 之前提取。
        """
        lines = []
        for msg in plan_loop.context.build_messages():
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            lines.append(text[:PLAN_SUMMARY_SNIPPET_LEN])
            if len(lines) >= PLAN_SUMMARY_MAX_TOOLS:
                break
        if not lines:
            return ""
        return "【阶段1已执行】\n" + "\n".join(lines)

    def _step_progress_summary(self, step: int) -> str:
        """当前已完成的中间结果摘要（取最近几轮工具结果），用于进度写回。"""
        tool_msgs = [m for m in self.context.build_messages() if m["role"] == "tool"]
        parts = []
        for m in tool_msgs[-PROGRESS_SNIPPET_ROUNDS:]:
            content = m.get("content")
            text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            parts.append(text[:PROGRESS_SNIPPET_LEN])
        joined = "；".join(parts)
        return f"步骤 {step} 后已完成：{joined or '暂无中间结果'}"

    def _execute_tool_call(self, tc: dict) -> str | None:
        """执行单个工具调用并回填 tool 结果上下文；返回中断说明或 None。"""
        name = tc["function"]["name"]
        args_json = tc["function"]["arguments"]
        key = (name, args_json)
        if key in self._storm:
            result = STORM_SUPPRESSED
        else:
            self._storm.append(key)
            result = self._call_tool(name, args_json)
        self.context.append("tool", json.dumps(result, ensure_ascii=False), tool_call_id=tc["id"])
        if self._is_structured_error(result):
            if result.get("reason") == "mode_permission":
                # 权限拒绝是模型误调用，不是工具故障：不计数、不中断
                pass
            elif name == self._last_error_tool:
                self._consecutive_failures += 1
            else:
                self._consecutive_failures = 1
                self._last_error_tool = name
            if self._consecutive_failures >= 2:
                return f"任务中断：工具 {name!r} 连续 2 次执行失败，无法继续"
        else:
            self._consecutive_failures = 0
            self._last_error_tool = None
        return None

    def _call_tool(self, name: str, args_json: str):
        """解析参数并调用工具；非法 JSON 用空 dict，未知/无权工具收口为结构化错误。

        权限在调用时由 registry.can_call(self.mode, name) 强制：模式无权调用
        返回 reason=="mode_permission" 的结构化错误，不进入真实执行。
        """
        if not self.registry.can_call(self.mode, name):
            return {
                "error": f"模式 {self.mode} 不允许调用工具 {name!r}",
                "reason": "mode_permission",
                "fix_hint": f"当前模式权限不足；切到有权限的模式（如 /investigate）后再试",
            }
        args = {}
        if args_json:
            try:
                args = json.loads(args_json)
            except (TypeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            return self.registry.call(name, args)
        except KeyError:
            return {
                "error": f"未知工具 {name!r}",
                "reason": "KeyError",
                "fix_hint": "检查工具名拼写后重试",
            }

    def _append_assistant(self, tool_calls: list[dict], reasoning_content: str | None = None) -> None:
        """按 OpenAI 协议追加带 tool_calls 字段的 assistant 消息（整批合成一条）。"""
        self.context.append(
            "assistant", content=None, tool_call_id=None,
            reasoning_content=reasoning_content,
        )
        last = self.context.build_messages()[-1]
        last["tool_calls"] = [
            {
                "id": tc["id"],
                "type": tc["type"],
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for tc in tool_calls
        ]

    @staticmethod
    def _is_structured_error(result) -> bool:
        return isinstance(result, dict) and "error" in result

    def _context_window(self) -> int:
        """当前 provider/model 的官方上下文窗口 token 数；查不到兜底 128K。"""
        try:
            cfg = get_provider(self.provider)
            if cfg:
                model_cfg = (cfg.get("models") or {}).get(self.model) or {}
                return int(model_cfg.get("context_length") or 128 * 1024)
        except Exception:  # noqa: BLE001 窗口查询是展示旁路，失败不阻塞
            pass
        return 128 * 1024

    def _record_usage(self, resp) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        self.last_usage = {"prompt_tokens": prompt, "completion_tokens": completion}
        self.total_tokens += prompt + completion
        self.prefix_hit_tokens += getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        self.prefix_miss_tokens += getattr(usage, "prompt_cache_miss_tokens", 0) or 0
        if self.bus is not None:
            self.bus.publish(
                EVENT_CONTEXT_USAGE,
                used_tokens=(self.last_usage or {}).get("prompt_tokens", 0),
                total_tokens=self._context_window(),
            )

    def _record_telemetry(
        self,
        resp,
        step: int,
        cache_hit: bool,
        semantic_cache_hit: bool = False,
        semantic_cache_miss: bool = False,
    ) -> None:
        """把本轮用量写进 telemetry（旁路：失败绝不打断主流程）。

        exact/semantic cache 命中时 resp 为 None，tokens 全 0。cache_hit/miss
        字段从 resp.usage 取，缺省按 0（退化按全 miss 计费）。semantic_cache_hit
        /semantic_cache_miss 标记语义缓存的命中/未命中路径。
        """
        if self.telemetry is None:
            return
        usage = getattr(resp, "usage", None)
        prompt = completion = hit = miss = 0
        reasoning_tokens = None
        if usage is not None:
            prompt = getattr(usage, "prompt_tokens", 0) or 0
            completion = getattr(usage, "completion_tokens", 0) or 0
            hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            details = getattr(usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
        try:
            self.telemetry.record(
                {
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "model": self.model,
                    "mode": self.mode,
                    "step": step,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "prompt_cache_hit_tokens": hit,
                    "prompt_cache_miss_tokens": miss,
                    "cache_hit": cache_hit,
                    "semantic_cache_hit": semantic_cache_hit,
                    "semantic_cache_miss": semantic_cache_miss,
                    "reasoning_tokens": reasoning_tokens,
                }
            )
        except Exception:  # noqa: BLE001  telemetry 是旁路
            pass

    @staticmethod
    def _normalize_tool_calls(message) -> list[dict]:
        """把 SDK 对象或 dict 形式的 tool_calls 归一化为 dict 列表。"""
        calls = []
        for tc in message.tool_calls or []:
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                calls.append(
                    {
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        },
                    }
                )
            else:
                calls.append(
                    {
                        "id": tc.id,
                        "type": getattr(tc, "type", "function"),
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )
        for c in calls:
            if not c.get("id"):
                c["id"] = f"tc_{uuid.uuid4().hex[:8]}"
        return calls

    def _scavenge_tool_calls(self, message) -> list[dict]:
        """从 reasoning_content/content 中回收形如 {name, arguments} 的工具 JSON。"""
        parts = []
        reasoning = getattr(message, "reasoning_content", None)
        content = getattr(message, "content", None)
        if reasoning:
            parts.append(reasoning)
        if content:
            parts.append(content)
        text = "\n".join(parts)
        if not text.strip():
            return []
        calls = []
        for block in _extract_json_blocks(text):
            try:
                data = json.loads(block)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if "name" not in data or "arguments" not in data:
                continue
            args = data["arguments"]
            if isinstance(args, dict):
                args_str = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = json.dumps(args)
            calls.append(
                {
                    "id": data.get("id") or f"call_scavenged_{len(calls)}",
                    "type": "function",
                    "function": {"name": data["name"], "arguments": args_str},
                }
            )
        return calls
