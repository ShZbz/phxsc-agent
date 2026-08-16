"""SKILL.md 扫描/解析 + 元数据表组装。

frontmatter 只取 name/description/version 三字段（--- 分隔 YAML 简化版），
损坏或缺关键字段的 SKILL.md 直接跳过（不报错）。缓存经济学铁律：元数据表
只进 system prompt（区1，启动组装一次）；skill 正文只走区2。
只用 stdlib（dataclasses, os, re, pathlib）。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_]*$")


@dataclass
class SkillMeta:
    """一个技能的最小元数据（来自 SKILL.md frontmatter）。"""

    name: str
    description: str
    version: str
    path: str  # SKILL.md 绝对路径


def _project_root() -> Path:
    """项目根目录（src/phxsc/skills/scan.py 向上四级）。"""
    return Path(__file__).resolve().parents[3]


DEFAULT_PROJECT_SKILLS = str(_project_root() / "skills")
DEFAULT_USER_SKILLS = str(Path.home() / ".phxsc" / "skills")


def skill_dirs(project_dir: str | None = None, user_dir: str | None = None) -> list[str]:
    """返回 [项目skills, 用户skills]；目录不存在跳过。

    用户级路径优先级：显式 user_dir > PHXSC_SKILLS 环境变量 > ~/.phxsc/skills
    （环境变量覆盖用户级路径，测试用）。
    """
    project = project_dir or DEFAULT_PROJECT_SKILLS
    user = user_dir or os.environ.get("PHXSC_SKILLS") or DEFAULT_USER_SKILLS
    return [d for d in (project, user) if os.path.isdir(d)]


def _parse_frontmatter(text: str) -> dict | None:
    """--- 分隔 frontmatter 简化解析：{key: value}；损坏返回 None。"""
    lines = text.splitlines()
    if not lines or not lines[0].strip().startswith("---"):
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip().startswith("---"):
            end = i
            break
    if end is None:  # 只有开头 ---、无收尾 → 截断，判损坏
        return None
    data: dict[str, str] = {}
    for line in lines[1:end]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:  # 非注释行无冒号 → 非 KV，判损坏
            return None
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def parse_skill_md(path) -> SkillMeta | None:
    """解析单个 SKILL.md → SkillMeta；损坏/缺字段/非法 name → None（跳过不报错）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm = _parse_frontmatter(text)
    if fm is None:
        return None
    name = fm.get("name", "").strip()
    if not _NAME_RE.match(name):
        return None
    description = fm.get("description", "").strip()
    if not description:
        return None
    version = str(fm.get("version", "")).strip()
    return SkillMeta(
        name=name,
        description=description,
        version=version,
        path=str(Path(path).resolve()),
    )


def scan_skills() -> list[SkillMeta]:
    """扫项目/用户两个目录下的 */SKILL.md，去重：同名时用户级优先。

    用户级先扫、先到先得（同名项目级不覆盖），且用户级条目排在列表前部。
    """
    metas: dict[str, SkillMeta] = {}
    for d in reversed(skill_dirs()):  # [用户, 项目]
        for md in sorted(Path(d).glob("*/SKILL.md")):
            meta = parse_skill_md(md)
            if meta is not None and meta.name not in metas:
                metas[meta.name] = meta
    return list(metas.values())


def build_metadata_table(metas: list[SkillMeta]) -> str:
    """格式化元数据表（system prompt 用）；每行 ≤160 字符，description 截断。"""
    if not metas:
        return ""
    lines = ["可用技能（任务匹配时可用 skill_load 工具加载全文）："]
    for m in metas:
        prefix = f"- {m.name}: "
        budget = 160 - len(prefix)
        desc = m.description
        if len(desc) > budget:
            desc = desc[: max(0, budget - 3)] + "..."
        lines.append(f"{prefix}{desc}")
    return "\n".join(lines)
