"""SKILL.md 正文加载（skill_load 工具与 /skill load 共用）。

正文全文返回，不截断；resources 列出 SKILL.md 同目录下 references/scripts/templates
子目录的文件名。注入 user 首行时全文注入（总量由 /skill load 时的 8KB 警告阈值提示，
不截断）。
只用 stdlib（dataclasses, pathlib）。
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillBody:
    """加载后的技能正文 + 资源清单。"""

    name: str
    content: str
    resources: list[str]  # references/scripts/templates 子目录下的文件名


def _resource_files(skill_dir: Path) -> list[str]:
    """列出三个资源子目录下的文件名（子目录存在才列，目录不存在跳过）。"""
    files: list[str] = []
    for sub in ("references", "scripts", "templates"):
        d = skill_dir / sub
        if d.is_dir():
            files.extend(sorted(p.name for p in d.iterdir() if p.is_file()))
    return files


def load_skill_body(name: str, metas) -> SkillBody | None:
    """按 name 找 meta → 读 SKILL.md 全文（UTF-8，不截断）；未找到 → None。"""
    meta = next((m for m in metas if m.name == name), None)
    if meta is None:
        return None
    path = Path(meta.path)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return SkillBody(name=name, content=content, resources=_resource_files(path.parent))
