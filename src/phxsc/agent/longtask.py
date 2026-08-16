"""长任务 plan-then-execute 两阶段触发启发式与计划文件产出（PLAN.md §4.11）。

触发启发式（PHXSC_LONGTASK=0 禁用，默认开启；全部常量可配置）：
- 显式触发词：TRIGGER_WORDS（规划/分步/先计划/综述/调研/研究/分析/整理/梳理）
- 多目标：逗号/顿号/加号/分号等分隔出的子目标 >= MULTI_GOAL_MIN
- 长输入：字符数 > LENGTH_THRESHOLD

产出：<workdir>/plans/<时间戳>-<清洗标题>.md（safe_write_path 校验，文件名清洗
仿 notes._clean_title；含 ../ 穿越片段直接拒绝）。执行进度由 append_progress
追加写回（不覆盖原计划），作为 Lost in the Middle 对策。
只用 stdlib + 现有模块（sandbox/paths），不引第三方依赖。
"""

import os
import re
from datetime import datetime
from pathlib import Path

from phxsc.sandbox.paths import safe_write_path

PLANS_DIR = "plans"

# 显式触发词（命中即走两阶段）
TRIGGER_WORDS = ("规划", "分步", "先计划", "综述", "调研", "研究", "分析", "整理", "梳理")

# 多目标分隔符与阈值
MULTI_GOAL_SEPARATORS = re.compile(r"[，,、+;；\n]")
MULTI_GOAL_MIN = 3

# 长输入阈值（字符数）
LENGTH_THRESHOLD = 200

# 简单任务豁免阈值（batch77）：触发词命中的短输入且实质内容过短 → 降级普通轮
SIMPLE_TASK_LEN = 20

LONGTASK_ENV = "PHXSC_LONGTASK"

# 简单任务豁免：去掉触发词与虚词后的剩余实质内容 < 6 字符才豁免
_SIMPLE_FILLER_RE = re.compile(r"(帮我|请|一下|麻烦|给我|能不能|可以)")


def _stripped_task_text(text: str) -> str:
    """去掉触发词与虚词后的剩余实质内容（用于简单任务豁免判断）。"""
    stripped = text
    for word in TRIGGER_WORDS:
        stripped = stripped.replace(word, "")
    stripped = _SIMPLE_FILLER_RE.sub("", stripped)
    return stripped.strip()


PLAN_PROMPT_TEMPLATE = (
    "这是一个复杂任务。第一步：先输出完整的执行步骤清单（只输出清单，不要开始执行）。\n"
    "清单要求：\n"
    "- 条数：3 到 10 条，建议 5-8 条。即使任务看起来简单也要拆成子任务；拿不准时也要排清单。\n"
    "- 每条是一个任务级步骤（如'调研钙钛矿热降解机理'），禁止工具调用级写法（如'search arxiv 钙钛矿'）；同类动作合并为一步。\n"
    "- 步骤只覆盖用户原始任务，禁止添加用户未要求的阶段（如 PPT 生成/深度汇报）。\n"
    "- 格式：每行一条 `1. 任务名`（阿拉伯数字+英文句点+空格），禁止表格/加粗/中文序号/嵌套列表。\n"
    "清单输出完成后，再进行只读侦察：可以搜索文献、阅读资料来支撑计划，但不要执行下载、写入笔记等操作。\n"
    "原始任务：\n{user_input}"
)


def longtask_enabled() -> bool:
    """开关：PHXSC_LONGTASK=0 禁用，其余值（含缺省）开启。"""
    return os.environ.get(LONGTASK_ENV, "1") != "0"


def is_long_task(user_input: str) -> bool:
    """启发式判断是否值得两阶段：显式触发词 / 多目标 / 长输入。

    batch77 简单任务豁免：触发词命中但多目标 <2 段时，去除触发词与虚词后
    剩余实质内容 <6 字符且原输入 ≤SIMPLE_TASK_LEN → 视为简单任务降级普通轮
    （不排 task 清单）；多目标 ≥MULTI_GOAL_MIN / 长输入 >LENGTH_THRESHOLD
    仍进两阶段。
    """
    if not longtask_enabled():
        return False
    text = (user_input or "").strip()
    if not text:
        return False
    triggered = any(word in text for word in TRIGGER_WORDS)
    goals = [seg for seg in MULTI_GOAL_SEPARATORS.split(text) if seg.strip()]
    if len(goals) >= MULTI_GOAL_MIN:
        return True
    if len(text) > LENGTH_THRESHOLD:
        return True
    if triggered:
        if len(goals) >= 2:
            return True
        if len(_stripped_task_text(text)) < 6 and len(text) <= SIMPLE_TASK_LEN:
            return False
        return True
    return False


def _clean_title(title: str) -> str:
    """标题 → 安全文件名：去路径分隔符/../非法字符，强制 .md 后缀（仿 notes._clean_title）。"""
    cleaned = title.strip()
    cleaned = cleaned.replace("\\", "_").replace("/", "_")
    cleaned = re.sub(r"[^\w\s()\-.]", "_", cleaned)
    cleaned = cleaned.replace("..", "")
    cleaned = cleaned.strip("_. ")
    if not cleaned:
        cleaned = "task"
    if not cleaned.endswith(".md"):
        cleaned += ".md"
    return cleaned


def _plan_filename(user_input: str) -> str:
    """计划文件名：<时间戳>-<清洗标题>，标题过长截断；含路径穿越片段（..）直接拒绝。"""
    raw = user_input.strip()
    if any(seg == ".." for seg in re.split(r"[\\/]", raw)):
        raise ValueError("计划标题包含路径穿越片段（..），已拒绝写入")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = _clean_title(raw)[:30]
    if slug.endswith(".md"):
        slug = slug[:-3]
    return f"{ts}-{slug}.md"


def default_workdir() -> str:
    """默认 workdir：PHXSC_WORKDIR 环境变量优先，默认 <项目根>/workspace。"""
    env = os.environ.get("PHXSC_WORKDIR")
    if env:
        return env
    return str(Path(__file__).resolve().parents[3] / "workspace")


def save_plan(workdir: str, user_input: str, plan_text: str) -> str:
    """把计划写入 <workdir>/plans/<文件名>.md；返回相对路径 plans/<文件名>。

    safe_write_path 校验逃逸；文件名清洗避免路径穿越。文件带「执行进度」节，
    供 append_progress 追加写回（不覆盖原计划）。
    """
    rel_dir = os.path.join(workdir, PLANS_DIR)
    os.makedirs(rel_dir, exist_ok=True)
    fname = _plan_filename(user_input)
    rel = os.path.join(PLANS_DIR, fname)
    target = safe_write_path(rel, workdir)
    content = (
        f"# 执行计划\n\n"
        f"## 原始任务\n{user_input}\n\n"
        f"## 计划\n{plan_text}\n\n"
        f"## 执行进度\n"
    )
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return rel


def append_progress(plan_path: str, step_summary: str) -> None:
    """把一步的结论摘要追加到计划文件末尾（「执行进度」节），不覆盖原计划。"""
    with open(plan_path, "a", encoding="utf-8") as f:
        f.write(f"- {step_summary}\n")
