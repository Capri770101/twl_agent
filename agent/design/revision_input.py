"""把已有方案及用户反馈解析成改版输入，不生成方案也不调用模型。"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from agent.design.quantities import _extract_budget
from agent.knowledge import query_knowledge


def _parse_plan(plan: str) -> dict:
    """尽力解析传入的方案（可能是 JSON 字符串或含 JSON 的文本）。"""
    if isinstance(plan, dict):
        return plan
    text = plan.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}


def _dims_from_plan(plan: dict) -> dict[str, str]:
    """从已有方案反推设计维度，供迭代时复用。"""
    dims: dict[str, str] = {}
    if plan.get('recipient'):
        dims['recipient'] = plan['recipient']
    if plan.get('occasion'):
        dims['occasion'] = plan['occasion']
    if plan.get('style_id'):
        dims['style'] = plan['style_id']
    if plan.get('substyle_id'):
        dims['substyle'] = plan['substyle_id']
    if plan.get('scene_id'):
        dims['scene'] = plan['scene_id']
    if plan.get('budget_num') is not None:
        dims['budget'] = str(plan['budget_num'])
    elif plan.get('budget_tier'):
        for t in query_knowledge('budget', '')['results']:
            if t['label'] == plan['budget_tier']:
                dims['budget'] = str(t['range'][0])
                break
    design = plan.get('design', {})
    main = [m['name'] for m in design.get('main_flowers', [])]
    if main:
        dims['_keep_main'] = ','.join(main)
    return dims


def _extract_feedback(feedback: str, *, style_keywords: dict[str, str],
                      color_keywords: dict[str, str], mood_keywords: dict[str, str],
                      flower_names: Callable[[], list[str]]) -> dict[str, Any]:
    """解析自然语言反馈为维度覆盖与需移除花材。"""
    dims: dict[str, str] = {}
    exclude: set[str] = set()
    budget_text = re.sub(r'(预算|价格)\s*(?:改为|改成|调整为|调整到|降到|降为|提高到|增加到|设为|调到)', r'\1', feedback)
    _, amount, _, _ = _extract_budget(budget_text)
    if amount is not None:
        dims['budget'] = str(int(amount)) if amount.is_integer() else str(amount)
    for kw, val in style_keywords.items():
        if kw in feedback:
            dims['style'] = val
            break
    for kw, val in color_keywords.items():
        if kw in feedback:
            dims['color'] = val
            break
    for kw, val in mood_keywords.items():
        if kw in feedback:
            dims['mood'] = val
            break
    for name in flower_names():
        if any(seg in feedback for seg in (f'不要{name}', f'去掉{name}', f'别用{name}', f'换掉{name}', f'去掉{name}花')):
            exclude.add(name)
    return {'dims': dims, 'exclude': exclude}
