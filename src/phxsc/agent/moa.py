"""MoA 核心协调层：共享防重登记表 + worker 池并行执行 + 结果协议 + 失败降级。

SharedSeenSet：线程安全的 source_id 防重登记表（arXiv id / DOI / URL）。
_run_worker：单个 worker 的 mini-loop（最多 MINI_LOOP_MAX_ROUNDS 轮 LLM 调用，
带工具执行；工具结果中的 source_id 先经 seen 登记，已存在则标 [SKIP] 并追加
跳过提示），返回固定结果协议；tools_enabled=False 时为纯文本单轮模式（生成场景）。
MoaRunner：ThreadPoolExecutor 并行跑多个 worker，一一对应 worker_cfgs 与
subtasks，按序返回结果协议（含 failed 项，不抛）。

超时语义：整体 worker 超时由 MoaRunner.run 的 future.result(timeout) 执行；
_run_worker 的 timeout 参数同时作为每次 create 的请求超时透传。
run_moa 支持 task_type="generate"：章节拆解 → 纯文本并行撰写 → 章节拼接聚合 →
最终文档落盘 workspace/notes/moa_<时间戳>.md（不自动出 PPT）。
只用 stdlib + openai client + 现有 providers/agent.tools，不引第三方依赖。
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from phxsc.providers import build_client
from phxsc.sandbox.paths import safe_write_path

MAX_WORKERS = 4
DEFAULT_TIMEOUT = 60.0
MINI_LOOP_MAX_ROUNDS = 3
WORKER_MAX_TOKENS = 2000

WORKER_MODE = "investigate"

# 主控常量：拆解/聚合单次非流式调用（固定模板，前缀缓存友好）
DECOMPOSE_MAX_TOKENS = 800
AGGREGATE_MAX_TOKENS = 1500

_DECOMPOSE_TEMPLATE = (
    "你是任务拆解主控。任务类型：{task_type}（survey=调研 / qa=问答）。把下面任务拆成\n"
    "{n} 个互不重叠的子任务（每个子任务给明确检索关键词/侧面，避免重复），仅输出 JSON 数组\n"
    '["子任务1", ...]：\n{question}'
)

_DECOMPOSE_TEMPLATE_GEN = (
    "你是文档拆解主控。任务类型：{task_type}（generate=文档/PPT 章节撰写）。把下面任务拆成\n"
    "{n} 个章节的撰写指令（每个指令明确章节主题与内容要求，n 个章节覆盖全部主题），仅输出 JSON 数组\n"
    '["章节撰写指令1", ...]：\n{question}'
)

_AGGREGATE_TEMPLATE = (
    "你是聚合主控。综合以下 {n} 个助手的回答，给出最终答案。要求：1) 共识点放最前；\n"
    "2) 各助手的分歧点明确列出（标注哪几个助手持哪种观点）；3) 不要简单拼接，要有综合。\n\n"
    "问题：{question}\n\n{answers}"
)

_AGGREGATE_TEMPLATE_GEN = (
    "你是文档聚合主控。以下 {n} 个助手各撰写了一个章节，请按章节顺序拼接成连贯文档。\n"
    "要求：1) 保留各章节全部内容要点，不删减；2) 章节间加过渡衔接，语气一致；\n"
    "3) 以标题分节组织。\n\n"
    "主题：{question}\n\n{answers}"
)

MAX_SOURCES_SHOWN = 10  # 最终文本来源列表最多展示条数

NOTES_DIR = "notes"  # 生成文档落盘目录（<workdir>/notes/）

# source_id 识别模式：arXiv 前缀（新旧格式）/ 4 位年份编号 / DOI / URL
_SOURCE_ID_PATTERNS = [
    re.compile(r"arXiv:\s*[^\s\"'<>)\]}]+", re.IGNORECASE),
    re.compile(r"(?<![\d.])\d{4}\.\d{4,5}(?:v\d+)?(?![.\d])"),
    re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]}]+"),
    re.compile(r"https?://[^\s\"'<>)\]}]+"),
]

SKIP_NOTE_TEMPLATE = "（注意：{n} 条文献已被其他 worker 收录，跳过）"


def _failed(
    provider: str,
    error: str,
    sources: list[str] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """构造 failed 结果协议。"""
    return {
        "status": "failed",
        "provider": provider,
        "answer": "",
        "sources": list(dict.fromkeys(sources or [])),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": error,
    }


class SharedSeenSet:
    """线程安全的 source_id 防重登记表（source_id = arXiv id / DOI / URL）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def register(self, source_id: str) -> bool:
        """首次登记返回 True，已存在返回 False。"""
        with self._lock:
            if source_id in self._ids:
                return False
            self._ids.add(source_id)
            return True

    def contains(self, source_id: str) -> bool:
        with self._lock:
            return source_id in self._ids

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._ids)


def _to_text(result) -> str:
    """工具结果 → 回填文本：str 原样，list/dict 等 json 序列化。"""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _extract_source_ids(text: str) -> list[str]:
    """多模式提取 source_id（非重叠、按出现顺序、去重）。"""
    spans = []
    for pattern in _SOURCE_ID_PATTERNS:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), m.group(0).strip()))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    ids: list[str] = []
    last_end = -1
    for start, end, sid in spans:
        if start < last_end:
            continue
        if sid and sid not in ids:
            ids.append(sid)
        last_end = end
    return ids


def _filter_and_mark(text: str, seen: SharedSeenSet) -> tuple[str, list[str]]:
    """登记 source_id：新 id 登记返回；已存在标 [SKIP] 并在末尾追加跳过提示。

    返回 (标注后的文本, 本 worker 新登记的 id 列表)。
    """
    new_ids: list[str] = []
    skipped = 0
    for sid in _extract_source_ids(text):
        if seen.register(sid):
            new_ids.append(sid)
        else:
            skipped += 1
            text = text.replace(sid, f"{sid} [SKIP]", 1)
    if skipped:
        text += f"\n{SKIP_NOTE_TEMPLATE.format(n=skipped)}"
    return text, new_ids


def _assistant_msg(msg, tool_calls) -> dict:
    """LLM 响应消息 → messages 里的 assistant dict（含 tool_calls）。"""
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": getattr(tc, "type", "function"),
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ],
    }


def _ok(
    provider: str,
    answer: str,
    sources: list[str] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """构造 ok 结果协议。"""
    return {
        "status": "ok",
        "provider": provider,
        "answer": answer,
        "sources": list(dict.fromkeys(sources or [])),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": "",
    }


def _run_worker(
    cfg: dict,
    subtask: str,
    registry,
    seen: SharedSeenSet,
    timeout: float,
    tools_enabled: bool = True,
) -> dict:
    """执行单个 worker 的 mini-loop，返回结果协议（固定结构）。

    cfg = {"name": <provider>, "model": <model>}（build_client 参数），
    可选 "tools_enabled": False（纯文本模式，供 generate 场景使用）。
    tools_enabled=True（默认）：最多 MINI_LOOP_MAX_ROUNDS 轮非流式调用
    （max_tokens=2000），每轮带 investigate 工具；上轮有 tool_calls 则执行后
    回填 role="tool" 继续，无 tool_calls 则结束。工具结果先过 seen 登记（防重）。
    tools_enabled=False：单轮纯文本调用（不带 tools 参数），结果直接作为 answer，
    无 seen 登记（生成场景无文献）。
    整体超时由调用方（MoaRunner.run 的 future.result）执行。
    """
    provider = cfg.get("name", "")
    prompt_tokens = 0
    completion_tokens = 0
    sources: list[str] = []
    try:
        client, _, model = build_client(provider, cfg.get("model"))
    except Exception as exc:
        return _failed(provider, f"provider 构建失败: {exc}")
    messages = [{"role": "user", "content": subtask}]
    try:
        if not tools_enabled:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=WORKER_MAX_TOKENS,
                timeout=timeout,
            )
            usage = getattr(resp, "usage", None)
            prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            answer = getattr(resp.choices[0].message, "content", "") or ""
            return _ok(provider, answer, [], prompt_tokens, completion_tokens)
        tools = registry.get_tools(WORKER_MODE)
        answer = ""
        for _ in range(MINI_LOOP_MAX_ROUNDS):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=WORKER_MAX_TOKENS,
                tools=tools,
                timeout=timeout,
            )
            usage = getattr(resp, "usage", None)
            prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)
            answer = msg.content or ""
            if not tool_calls:
                break
            messages.append(_assistant_msg(msg, tool_calls))
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = registry.call(tc.function.name, args)
                except Exception as exc:
                    result = {
                        "error": f"工具调用失败: {exc}",
                        "reason": type(exc).__name__,
                        "fix_hint": "检查工具参数后重试",
                    }
                text, new_ids = _filter_and_mark(_to_text(result), seen)
                sources.extend(new_ids)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": text}
                )
        return _ok(provider, answer, sources, prompt_tokens, completion_tokens)
    except Exception as exc:
        return _failed(provider, str(exc), sources, prompt_tokens, completion_tokens)


class MoaRunner:
    """并行执行器：worker_cfgs[i] 执行 subtasks[i]，按序返回结果协议列表。"""

    def __init__(self, registry, timeout: float = DEFAULT_TIMEOUT, max_workers: int = MAX_WORKERS):
        self.registry = registry
        self.timeout = timeout
        self.max_workers = max_workers

    def run(
        self,
        worker_cfgs: list[dict],
        subtasks: list[str],
        seen: SharedSeenSet | None = None,
    ) -> list[dict]:
        """并行执行（含 failed 项，不抛 worker 异常）；长度不等 / 超上限 → ValueError。"""
        if len(worker_cfgs) != len(subtasks):
            raise ValueError(
                f"worker_cfgs 与 subtasks 长度不等：{len(worker_cfgs)} != {len(subtasks)}"
            )
        if len(worker_cfgs) > MAX_WORKERS:
            raise ValueError(f"worker 数量超出上限：{len(worker_cfgs)} > {MAX_WORKERS}")
        if seen is None:
            seen = SharedSeenSet()
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = [
                ex.submit(
                    _run_worker, cfg, subtask, self.registry, seen, self.timeout,
                    cfg.get("tools_enabled", True),
                )
                for cfg, subtask in zip(worker_cfgs, subtasks)
            ]
            results = []
            for future, cfg in zip(futures, worker_cfgs):
                try:
                    results.append(future.result(timeout=self.timeout))
                except TimeoutError:
                    results.append(
                        _failed(cfg.get("name", ""), f"worker 执行超时（>{self.timeout}s）")
                    )
                except Exception as exc:
                    results.append(_failed(cfg.get("name", ""), str(exc)))
        return results


def _extract_json_array(text: str) -> list[str] | None:
    r"""响应文本 → JSON 字符串数组；解析失败返回 None。

    先尝试全文直接解析（以 [ 开头），再退到正则 \[.*\] 提取（兼容 code fence 包裹）。
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped.startswith("["):
        candidates.append(stripped)
    candidates.extend(re.findall(r"\[.*\]", text, re.DOTALL))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [str(x) for x in data]
    return None


def _plan_decompose(client, model: str, task_type: str, question: str, n_workers: int) -> list[str]:
    """主控单次非流式调用拆解任务 → 恰好 n_workers 个子任务。

    generate 类型用章节撰写指令模板（_DECOMPOSE_TEMPLATE_GEN），其余用原模板。
    解析失败 → n 个相同子任务兜底（附加"角度{i}"区分）；子任务数截断到
    n_workers，不足则用"角度{i}"补齐（保证与 worker 数对齐）。
    """
    template = (
        _DECOMPOSE_TEMPLATE_GEN if str(task_type) == "generate" else _DECOMPOSE_TEMPLATE
    )
    prompt = (
        template.replace("{task_type}", str(task_type))
        .replace("{n}", str(n_workers))
        .replace("{question}", question)
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=DECOMPOSE_MAX_TOKENS,
    )
    text = getattr(resp.choices[0].message, "content", "") or ""
    subtasks = [s for s in (_extract_json_array(text) or []) if s.strip()]
    while len(subtasks) < n_workers:
        subtasks.append(f"{question}（角度{len(subtasks) + 1}）")
    return subtasks[:n_workers]


def _format_answers(results: list[dict]) -> str:
    """编号答案列表（含 provider 名，failed 标注"未响应"），聚合模板填充用。"""
    lines = []
    for i, r in enumerate(results, 1):
        provider = r.get("provider", "?")
        if r.get("status") == "ok":
            lines.append(f"{i}. [{provider}] {r.get('answer', '')}")
        else:
            lines.append(f"{i}. [{provider}] 未响应（{r.get('error', '')}）")
    return "\n".join(lines)


def _aggregate(client, model: str, question: str, results: list[dict]) -> str:
    """聚合主控单次调用：编号答案列表（含 provider 名，failed 标注"未响应"）。"""
    answers = _format_answers(results)
    prompt = (
        _AGGREGATE_TEMPLATE.replace("{n}", str(len(results)))
        .replace("{question}", question)
        .replace("{answers}", answers)
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=AGGREGATE_MAX_TOKENS,
    )
    return getattr(resp.choices[0].message, "content", "") or ""


def _aggregate_gen(client, model: str, question: str, results: list[dict]) -> str:
    """生成场景聚合主控单次调用：各章节按序拼接为连贯文档（保留各章节内容）。"""
    answers = _format_answers(results)
    prompt = (
        _AGGREGATE_TEMPLATE_GEN.replace("{n}", str(len(results)))
        .replace("{question}", question)
        .replace("{answers}", answers)
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=AGGREGATE_MAX_TOKENS,
    )
    return getattr(resp.choices[0].message, "content", "") or ""


def _workdir() -> str:
    """workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace；确保 notes/ 存在。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        workdir = env
    else:
        workdir = str(Path(__file__).resolve().parents[3] / "workspace")
    os.makedirs(os.path.join(workdir, NOTES_DIR), exist_ok=True)
    return workdir


def _save_generated(text: str) -> str:
    """生成文档落盘 <workdir>/notes/moa_<时间戳>.md，返回"已生成：<路径>\\n\\n<预览>…"。"""
    workdir = _workdir()
    fname = f"moa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    rel = os.path.join(NOTES_DIR, fname)
    os.makedirs(os.path.join(workdir, NOTES_DIR), exist_ok=True)
    target = safe_write_path(rel, workdir)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    preview = text[:200] + ("…" if len(text) > 200 else "")
    return f"已生成：{rel}\n\n{preview}"


def run_moa(client, model: str, registry, task_type: str, question: str,
            worker_cfgs: list[dict]) -> str:
    """MoA 模块级入口：_plan_decompose → MoaRunner.run（seen 共享）→ _aggregate → 组装最终文本。

    worker_cfgs = [{"name": <provider>, "model": <model>}, ...]（MoaRunner 协议）。
    task_type="generate"：章节拆解 → 纯文本并行撰写（cfg 注入 tools_enabled=False）
    → _aggregate_gen 拼接 → 落盘 workspace/notes/moa_<时间戳>.md，返回路径+预览
    （不自动出 PPT）。其余类型行为不变。
    全部 failed → 失败文案，不调聚合。
    """
    if not worker_cfgs:
        return "MoA 执行失败：无可用 worker"
    n = len(worker_cfgs)
    subtasks = _plan_decompose(client, model, task_type, question, n)
    subtasks = subtasks[:n]
    while len(subtasks) < n:
        subtasks.append(f"{question}（角度{len(subtasks) + 1}）")
    seen = SharedSeenSet()
    if task_type == "generate":
        gen_cfgs = [{**cfg, "tools_enabled": False} for cfg in worker_cfgs]
        results = MoaRunner(registry).run(gen_cfgs, subtasks, seen)
        if not any(r.get("status") == "ok" for r in results):
            reasons = "；".join(dict.fromkeys(str(r.get("error", "")) for r in results))
            return f"MoA 执行失败：所有助手未响应（{reasons}）"
        document = _aggregate_gen(client, model, question, results)
        return _save_generated(document)
    results = MoaRunner(registry).run(worker_cfgs, subtasks, seen)
    if not any(r.get("status") == "ok" for r in results):
        reasons = "；".join(dict.fromkeys(str(r.get("error", "")) for r in results))
        return f"MoA 执行失败：所有助手未响应（{reasons}）"
    aggregated = _aggregate(client, model, question, results)
    providers = "、".join(cfg.get("name", "") for cfg in worker_cfgs)
    sources = list(
        dict.fromkeys(sid for r in results for sid in r.get("sources", []) if sid)
    )
    if len(sources) > MAX_SOURCES_SHOWN:
        source_text = "、".join(sources[:MAX_SOURCES_SHOWN]) + (
            f"（共 {len(sources)} 条，截断显示前 {MAX_SOURCES_SHOWN}）"
        )
    else:
        source_text = "、".join(sources) or "无"
    usage: dict[str, list[int]] = {}
    for r in results:
        p = r.get("provider", "?")
        u = usage.setdefault(p, [0, 0])
        u[0] += r.get("prompt_tokens", 0) or 0
        u[1] += r.get("completion_tokens", 0) or 0
    usage_lines = "\n".join(
        f"  {p}: prompt_tokens={u[0]}, completion_tokens={u[1]}"
        for p, u in usage.items()
    )
    return (
        f"## MoA 综合结果（{n} 助手：{providers}）\n"
        f"{aggregated}\n"
        f"---\n"
        f"各助手已登记文献源：{source_text}\n"
        f"用量：\n{usage_lines}"
    )
