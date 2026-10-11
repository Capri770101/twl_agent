"""按预算与真实价表反推每种花材支数，保持显式支数约束优先。"""
from __future__ import annotations

_TIER_MAIN_STEMS = {'T1': 6, 'T2': 8, 'T3': 10, 'T4': 14, 'T5': 18, 'T6': 24}
_MAX_MAIN_STEMS = 99


def _price_table(shop_id: str = ''):
    """按店铺取花材价格表，未锁店时取平台基线。"""
    from agent.pricing import price_table
    return price_table(shop_id)


def _alloc_stems(tier: dict, main: list[dict], fillers: list[dict], foliage: list[dict],
                 budget_num: int | None = None, stem_count: int | None = None,
                 table=None) -> dict[str, int]:
    """预算减基础费后按综合单价反推数量；用户明确支数为硬约束。"""
    if table is None:
        table = _price_table()
    tkey = tier.get('tier', 'T2')
    if stem_count is not None and main:
        total_main = int(stem_count)
    else:
        total_main = _TIER_MAIN_STEMS.get(tkey, 10)
    if budget_num is None and stem_count is None and main:
        rng = list(tier.get('range') or [])
        if len(rng) == 2 and rng[1] > rng[0]:
            lo, hi = float(rng[0]), float(rng[1])
            mid = lo * 1.6 if hi > lo * 4 else (lo + hi) / 2
            budget_num = max(int(mid), 60)
    if budget_num is not None and main and stem_count is None:
        main_unit = sum(table.unit_price(m['name']) for m in main) / len(main)
        side = fillers + foliage
        side_ratio = 0.4 if fillers else 0.25
        side_unit = (sum(table.unit_price(f['name']) for f in side) / len(side)) if side else 0.0
        per_main_cost = main_unit + side_ratio * side_unit
        main_budget = max(0.0, float(budget_num) - table.base_fee)
        total_main = (max(1, int(main_budget / per_main_cost))
                      if per_main_cost > 0 else _TIER_MAIN_STEMS.get(tkey, 10))
    total_main = max(1, min(int(total_main), _MAX_MAIN_STEMS))
    per_main = max(1, total_main // max(len(main), 1))
    stems: dict[str, int] = {}
    for i, f in enumerate(main):
        stems[f['name']] = per_main + (1 if i < total_main % max(len(main), 1) else 0)
    side_total = max(1, round(total_main * (0.4 if fillers else 0.25)))
    side_pool = [f['name'] for f in fillers] + [f['name'] for f in foliage]
    if side_pool:
        per_side = max(1, side_total // len(side_pool))
        for i, f in enumerate(side_pool):
            stems[f] = per_side + (1 if i < side_total % len(side_pool) else 0)
    return stems
