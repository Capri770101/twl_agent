"""金额、支数和中文数字的确定性解析；不依赖 LLM 或方案状态。"""
from __future__ import annotations

import re
from typing import Any

_BUDGET_ORAL = {'一两百': 150, '一二百': 150, '小几百': 200, '两三百': 250, '二三百': 250, '三四百': 350, '三五百': 400, '五六百': 550, '七八百': 750, '千把块': 1000, '一千': 1000, '一两千': 1500, '两三千': 2500}
_MAGNITUDE_UNITS: dict[str, float] = {
    '万': 1e4, '萬': 1e4, 'w': 1e4, 'W': 1e4,
    '千': 1e3, 'k': 1e3, 'K': 1e3,
    '百': 1e2,
}
_AMOUNT_PAT = r'(\d{1,6}(?:\.\d{1,2})?)\s*([万千百wWkK])?'
_CN_DIGIT = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}


def _cn_to_int(raw: str) -> int | None:
    """中文数字转整数（支持两、十、十一、二十五等）。"""
    s = (raw or '').strip()
    if not s:
        return None
    if '十' not in s:
        return _CN_DIGIT.get(s)
    left, _, right = s.partition('十')
    tens = 1 if left == '' else _CN_DIGIT.get(left)
    ones = 0 if right == '' else _CN_DIGIT.get(right)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def _extract_budget(text: str) -> tuple[str | None, float | None, float | None, float | None]:
    """预算返回 (口语锚点、精确金额、软区间下界、软区间上界)。

    保留万/千/百的数量级，仅当价格信号明确时才将数字当作金额，
    防止「11 朵」或「两万朵」被误认成预算。
    """
    anchor: str | None = None
    for oral, oral_amount in _BUDGET_ORAL.items():
        if oral in text:
            text = text.replace(oral, f' {oral_amount} ')
            anchor = oral
            break
    for pat in (
        re.compile(_AMOUNT_PAT + r'\s*(?:元|块|块钱|rmb|¥|刀)', re.I),
        re.compile(r'(?:预算|价格)\s*' + _AMOUNT_PAT),
        re.compile(_AMOUNT_PAT + r'\s*(?:预算|价格)'),
        re.compile(_AMOUNT_PAT + r'\s*元左右'),
    ):
        m = pat.search(text)
        if m:
            num = float(m.group(1)) * _MAGNITUDE_UNITS.get(m.group(2) or '', 1.0)
            return (anchor, num, round(num * 0.8), round(num * 1.2))
    for pat in (
        re.compile(r'([一二两三四五六七八九十]{1,3})\s*([万千百])\s*(?:元|块|块钱)'),
        re.compile(r'(?:预算|价格)\s*([一二两三四五六七八九十]{1,3})\s*([万千百])'),
    ):
        cm = pat.search(text)
        if cm:
            base = _cn_to_int(cm.group(1))
            if base:
                num = float(base) * _MAGNITUDE_UNITS[cm.group(2)]
                return (anchor, num, round(num * 0.8), round(num * 1.2))
    return (anchor, None, None, None)


def _extract_stem_count(text: str) -> int | None:
    """只认明确的花材支数，不把「一束」等容器单位当数量。"""
    m = re.search(r'(\d{1,3})\s*(?:朵|支|枝|根|头)', text)
    if m:
        return max(1, min(int(m.group(1)), 999))
    if re.search(r'一\s*打', text):
        return 12
    cm = re.search(r'([一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])\s*(?:朵|支|枝|根|头)', text)
    if cm:
        val = _cn_to_int(cm.group(1))
        if val:
            return max(1, min(val, 999))
    return None


def _safe_float(value: Any) -> float | None:
    """宽松转 float；bool 不能当数字。"""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """宽松转 int；bool 不能当数字。"""
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
