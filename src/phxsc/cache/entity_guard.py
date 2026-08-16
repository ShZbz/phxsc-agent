"""实体差异守卫：两 query 的差异片段若含实体（化学式/年份/数值/型号/ID/中文数字），
判为语义不同，强制 miss。防止同形不同义污染语义缓存。

机制：difflib 在字符级与词级（空白分词）两个粒度取非 equal 差异片段；
字符级负责中文数字单字（三价/四价），词级负责完整实体 token（Mn3Sn/2025/GPT-5/2.0eV）
——单字符粒度会把实体碎成单字符（'2024'→'4'），正则无法命中完整 token。
纯语言改写/虚词/标点差异放行。
纯中文专名差异（方法名/材料名全中文，如"中子散射"vs"穆斯堡尔谱"）不拦截：
实测 embedding 余弦 0.9106 < 0.93 阈值，语义缓存自身不会命中，守卫无实际
作用；强行拦截会误伤已验证的同义改写命中（"研究现状"↔"研究进展" 0.9887）。
"""

import difflib
import re

ENTITY_PATTERNS = [
    r"[A-Z][a-z]?\d",
    r"[A-Za-z]+-\d",
    r"(?<!\d)\d{4}(?!\d)",          # 年份：\b 在 CJK 前失效，改 ASCII 数字 lookaround
    r"\d+\.\d+",
    r"(?<!\d)\d{2,}(?!\d)",          # 多位数：同上（第42章 vs 第43章）
    r"(arxiv|doi)\s*[:.]?\s*\S+",
    r"(?<![0-9A-Za-z])[A-Za-z](?![0-9A-Za-z])",        # 单字母标识符：材料A vs 材料B
    r"(?<![0-9A-Za-z])\d[A-Za-z](?![0-9A-Za-z])",      # 数字+字母：图3a vs 图3b
]

CN_NUMERAL = "一二三四五六七八九十"

_PATTERNS = [re.compile(p) for p in ENTITY_PATTERNS]


def _frag_matches(frag: str) -> bool:
    """差异片段是否含实体：任一正则命中，或 ≤2 字符且含中文数字。"""
    frag = frag.strip()
    if not frag:
        return False
    for pat in _PATTERNS:
        if pat.search(frag):
            return True
    if len(frag) <= 2 and any(c in CN_NUMERAL for c in frag):
        return True
    return False


def _diff_has_entity(a, b) -> bool:
    """对 a/b 做 difflib，任一非 equal 差异片段命中实体即返回 True。"""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "insert"):
            frag = "".join(b[j1:j2])
        else:  # delete
            frag = "".join(a[i1:i2])
        if _frag_matches(frag):
            return True
    return False


def entity_diff_guard(q1: str, q2: str) -> bool:
    """True=发现实体差异应 miss；False=可命中。完全相同字符串恒 False。"""
    if q1 == q2:
        return False
    if _diff_has_entity(q1, q2):
        return True
    if _diff_has_entity(q1.split(), q2.split()):
        return True
    return False
