"""DIY 预算档、花材费用明细与卡片报价保持同一来源。"""
from __future__ import annotations

import copy
import re

from agent.design.stem_allocation import _alloc_stems, _price_table
from agent.knowledge import query_knowledge


def _get_tier(budget_num: int | None, scene_anchor: str | None) -> dict:
    """显式预算优先，其次场景锚点，最后取默认档。"""
    all_tiers = query_knowledge('budget', '')['results']
    if budget_num is not None:
        t = next((t for t in all_tiers if t['range'][0] <= budget_num <= t['range'][1]), None)
        if t:
            return t
    if scene_anchor:
        t = next((t for t in all_tiers if t['tier'] == scene_anchor), None)
        if t:
            return t
    return all_tiers[1]


def _build_budget_breakdown(main: list[dict], fillers: list[dict], foliage: list[dict],
                            packaging: dict | None, tier: dict, budget_num: int | None,
                            stem_count: int | None = None, table=None) -> dict:
    """费用明细按单支价+基础费计算，逐项取整再求和。"""
    if table is None:
        table = _price_table()

    def cost(flowers: list[dict]) -> float:
        return sum(stems.get(f['name'], 1) * table.unit_price(f['name']) for f in flowers)

    stems = _alloc_stems(tier, main, fillers, foliage, budget_num, stem_count, table)
    main_cost, filler_cost, foliage_cost = cost(main), cost(fillers), cost(foliage)
    base_fee = float(table.base_fee)
    amounts = [round(main_cost), round(filler_cost), round(foliage_cost), round(base_fee)]
    total = sum(amounts)
    source_label = {'override': '商家设定的标准价', 'merchant': '本店在售商品',
                    'baseline': '平台在售商品'}.get(table.source, '平台在售商品')
    items = [
        {'item': '主花', 'detail': '、'.join(f"{m['name']}×{stems.get(m['name'], 1)}" for m in main) or '无', 'amount': amounts[0]},
        {'item': '配材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in fillers) or '无', 'amount': amounts[1]},
        {'item': '叶材', 'detail': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in foliage) or '无', 'amount': amounts[2]},
        {'item': '包装与手工', 'detail': '含包装材料与修剪扎制', 'amount': amounts[3]},
    ]
    return {
        'total_estimate': total,
        'currency': 'CNY',
        'items': items,
        'fees': {
            'base_fee': base_fee,
            'base_standard': f'包装、手工与基础服务 {base_fee:.0f} 元/束（按{source_label}推算）',
            'note': f'单支花材价按{source_label}推算，已含包装与手工；下单前请以门店确认为准。',
            'price_source': table.source,
        },
        'note': f'以上按{source_label}推算，实际价格以门店/供应商为准。',
    }


def _sync_plan_price(plan: dict, tier_label: str = '') -> dict:
    """将卡片顶层报价与最终明细保持一致（单位：元）。"""
    if not isinstance(plan, dict):
        return plan
    total = (plan.get('budget_breakdown') or {}).get('total_estimate')
    if total is None:
        return plan
    total = int(total)
    label = tier_label or _get_tier(plan.get('budget_num'), None).get('label', '')
    plan['price'] = total
    plan['price_unit'] = 'CNY'
    plan['price_text'] = f'约 {total} 元（{label}档）' if label else f'约 {total} 元'
    plan['estimated_price'] = plan['price_text']
    return plan


def _enrich_plan_fees(plan: dict) -> dict:
    """在用料最终确定后重算支数、单价、明细与顶层报价。

    返回深拷贝，不修改传入方案；未选出主花时保留原方案内容。
    所有 LLM 合并后的报价必须经过此处，不能信任模型给的价格。
    """
    plan = copy.deepcopy(plan)
    d = plan.get('design') or {}
    main = [m for m in d.get('main_flowers') or [] if isinstance(m, dict)]
    fillers = [f for f in d.get('fillers') or [] if isinstance(f, dict)]
    foliage = [g for g in d.get('foliage') or [] if isinstance(g, dict)]
    if not main:
        return plan
    table = _price_table(str(plan.get('shop_id') or ''))
    tier = _get_tier(plan.get('budget_num'), None)
    stems = _alloc_stems(tier, main, fillers, foliage, plan.get('budget_num'),
                         plan.get('stem_count'), table)
    for fl in main + fillers + foliage:
        fl['qty'] = stems.get(fl['name'], 1)
        fl['unit_price'] = round(table.unit_price(fl['name']), 1)
    _steps = plan.get('diy_steps') or []
    if isinstance(_steps, list):
        _new_steps: list[str] = []
        for _s in _steps:
            _t = str(_s)
            for _name, _q in stems.items():
                _t = re.sub(rf'{re.escape(_name)}\s*[×xX*]\s*\d+', f'{_name}×{_q}', _t)
            _new_steps.append(_t)
        plan['diy_steps'] = _new_steps
    source_label = {'override': '商家设定的标准价', 'merchant': '本店在售商品',
                    'baseline': '平台在售商品'}.get(table.source, '平台在售商品')
    d['fees'] = {
        'base_fee': table.base_fee,
        'base_standard': f'包装、手工与基础服务 {table.base_fee:.0f} 元/束（按{source_label}推算）',
        'stem_count': '、'.join(f"{f['name']}×{stems.get(f['name'], 1)}" for f in main + fillers + foliage),
        'note': f'单支价按{source_label}推算，含包装与手工；实际以门店确认为准。',
        'price_source': table.source,
        'price_samples': table.samples,
    }
    pkg_name = d.get('packaging') or '花束'
    pkg = {'name': pkg_name, 'id': 'PK_BOX' if '礼盒' in pkg_name else 'PK_BOUQUET'}
    plan['budget_breakdown'] = _build_budget_breakdown(main, fillers, foliage, pkg, tier,
                                                       plan.get('budget_num'),
                                                       plan.get('stem_count'), table)
    _sync_plan_price(plan, tier.get('label', ''))
    return plan
