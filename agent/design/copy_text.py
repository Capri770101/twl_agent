"""将已定型的 DIY 方案渲染成供用户复制给门店的需求清单。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger('tools')
_COPY_TEXT_MAX = 2000


def build_plan_copy_text(plan: dict) -> str:
    """根据真实方案明细生成纯文本清单，须在缺料标注之后调用。"""

    def _s(value: Any, default: str = '') -> str:
        return str(value).strip() if value not in (None, '') else default

    if not isinstance(plan, dict) or not plan:
        return ''
    raw_design = plan.get('design')
    design = raw_design if isinstance(raw_design, dict) else {}
    lines = ['【定制花束需求单】', f"方案：{_s(plan.get('name'), '定制花束')}"]

    head: list[str] = []
    if _s(plan.get('recipient')):
        head.append(f"对象：{_s(plan['recipient'])}")
    if _s(plan.get('occasion')):
        head.append(f"场合：{_s(plan['occasion'])}")
    style = _s(plan.get('substyle')) or _s(plan.get('style'))
    if style and style not in _s(plan.get('name')):
        head.append(f'风格：{style}')
    if head:
        lines.append('｜'.join(head))

    mid: list[str] = []
    colors = design.get('color_scheme') or []
    if colors:
        mid.append('配色：' + '/'.join(_s(c) for c in colors if _s(c)))
    if _s(design.get('packaging')):
        mid.append('包装：' + _s(design['packaging']))
    if mid:
        lines.append('｜'.join(mid))

    raw_fees = design.get('fees')
    fees = raw_fees if isinstance(raw_fees, dict) else {}
    if _s(fees.get('stem_count')):
        lines.append(f"用料：{_s(fees['stem_count'])}")

    raw_breakdown = plan.get('budget_breakdown')
    breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
    items = [it for it in (breakdown.get('items') or []) if isinstance(it, dict)]
    _FEE_ITEMS = ('人工费', '装饰费')
    _BARE_ITEMS = ('包装与手工', '基础费')
    details: list[str] = []
    fee_total = 0.0
    has_fee = False
    for it in items:
        item, detail = _s(it.get('item')), _s(it.get('detail'))
        amount = it.get('amount') if isinstance(it.get('amount'), (int, float)) else None
        if item in _FEE_ITEMS:
            has_fee = True
            fee_total += amount or 0
            continue
        if not amount:
            continue
        seg = item if item in _BARE_ITEMS else (
            f'{item}：{detail}' if item and detail else (item or detail))
        if seg:
            details.append(f'· {seg} —— {int(round(amount))} 元')
    if details:
        lines.append('')
        lines.append('费用参考')
        lines.extend(details)
        if has_fee and fee_total:
            lines.append(f'· 人工与装饰费 —— {int(round(fee_total))} 元')
    price = plan.get('price')
    if isinstance(price, (int, float)) and price:
        lines.append(f'合计约 {int(price)} 元（按平台在售价估算，实际以门店报价为准）')
    elif _s(breakdown.get('note')):
        lines.append(_s(breakdown['note']))

    missing = [m for m in (plan.get('unavailable_materials') or []) if _s(m)]
    if missing:
        lines.append('')
        lines.append('注：以下原料这家店当前暂无，可能需另外采购 —— ' + '、'.join(_s(m) for m in missing))

    lines.append('')
    lines.append('（本清单由 AI 花艺小助手按平台在售价生成，实际报价以门店为准）')
    text = '\n'.join(lines).strip()
    if len(text) > _COPY_TEXT_MAX:
        logger.warning('[design] copy_text 超长（%d 字符）已截断', len(text))
        text = text[:_COPY_TEXT_MAX].rstrip() + '…（清单较长，已截断）'
    return text
