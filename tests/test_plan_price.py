"""DIY 方案必须带**数值型价格**（外部安全审计 P0-3 修复，2026-09-18）。

事故链：flora 的 DIY 方案原本只有 `estimated_price` **字符串**（"约 300 元（轻送礼档）"）
与嵌套的 `budget_breakdown.total_estimate`，**没有顶层数值字段**。
接入方按平台商品卡惯例取 `plan['price']` → `undefined`
→ 前端 `Math.round((e.price || 0) * 100)` 算出 **0**
→ 一束约 300 元的花束以 **0 元**加入购物车并跳转结算页。

这里锁住 4 件事：
1. `price` 必须是**正**整数（无预算时也不能是 0/None）；
2. `price` 必须与 `budget_breakdown.total_estimate` 一致（明细与总价对得上）；
3. `price_text` 与 `estimated_price` **同口径**（不能"文案说 300、扣款 205"）；
4. `price_unit` 必须声明（单位是「元」，与平台商品卡的「分」不同，必须显式标注）。
"""

from __future__ import annotations

import pytest

from agent.tools import _build_plan

# 覆盖：有预算 / 无预算 / 不同场合
DIMS_CASES = [
    {'recipient': '母亲', 'occasion': '祝寿', 'budget': '300'},
    {'recipient': '恋人', 'occasion': '生日', 'budget': '200', 'colors': ['粉']},
    {'recipient': '朋友', 'occasion': '感谢'},          # 未给预算
    {'recipient': '长辈', 'occasion': '探病', 'budget': '800'},
]


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_price_is_positive_int(dims: dict) -> None:
    """downstream 靠这个字段算钱，绝不能是 None / 0 / 字符串。"""
    p = _build_plan(dims)
    assert 'price' in p, f'方案缺 price 字段（下游会算成 0 元）：{p.get("name")}'
    assert isinstance(p['price'], int) and not isinstance(p['price'], bool)
    assert p['price'] > 0, f'price 必须为正：{p.get("price")}'


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_price_matches_breakdown_total(dims: dict) -> None:
    """总价必须等于明细汇总 —— 否则用户对账会对不上。"""
    p = _build_plan(dims)
    assert p['price'] == p['budget_breakdown']['total_estimate']


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_price_text_consistent_with_numeric(dims: dict) -> None:
    """文案与数值同口径：不能出现「文案 300 元、字段 205」这种不一致。"""
    p = _build_plan(dims)
    assert p['price_text'] == p['estimated_price'], 'price_text 与 estimated_price 口径不一致'
    assert str(p['price']) in p['price_text'], '展示文案里没有出现真实数值'


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_price_unit_declared(dims: dict) -> None:
    """单位必须显式声明：DIY 是「元」，平台商品卡是「分」，极易混淆。"""
    p = _build_plan(dims)
    assert p['price_unit'] == 'CNY'


def test_price_not_zero_is_the_actual_regression() -> None:
    """回归本事故的核心断言：绝不能复现「0 元加购」。

    模拟下游前端的算法 `Math.round((e.price || 0) * 100)` ——
    修复前 `e.price` 为 undefined，结果就是 0（分），即 0 元。
    """
    p = _build_plan({'recipient': '母亲', 'occasion': '祝寿', 'budget': '300'})
    cents = round((p.get('price') or 0) * 100)
    assert cents > 0, '复现了 0 元加购事故'


# ══ 价格与明细的「重算同步」（2026-09-18 端到端实测发现的分叉） ══════════════
# 背景：`price` / `price_text` / `estimated_price` 原本只在 _build_plan 里写一次，
# 而 `budget_breakdown` 在 LLM 语义路径上会被重算 4 次（_merge_plan ×3 +
# _enrich_plan_fees ×1）。模型换过花材后，明细按新花材算、顶层价格却停在 baseline。
# 演示环境实测分叉：**明细合计 547 元，而 price = 258 元**。
# 危害方向很坏：price 是给下游结算用的 → **按便宜的价扣款、按真实的单备货**。

def _breakdown_sum(plan: dict) -> int:
    """明细逐项求和（用户和店家会当场这么对账）。"""
    return sum(int(round(it.get('amount') or 0))
               for it in (plan.get('budget_breakdown') or {}).get('items') or [])


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_breakdown_items_sum_equals_total(dims: dict) -> None:
    """明细加起来必须等于合计 —— 对不上会被当成"这家不靠谱"。"""
    p = _build_plan(dims)
    assert _breakdown_sum(p) == p['budget_breakdown']['total_estimate']


def _llm_plan_with_pricier_flowers() -> dict:
    """模拟 LLM 换上一批更贵的花材（这是分叉的触发条件）。"""
    return {
        'name': '法式·紫韵浪漫生日花束',
        'design': {
            'main_flowers': [{'name': '香槟玫瑰'}, {'name': '绣球'}, {'name': '唐菖蒲'}],
            'fillers': [{'name': '洋桔梗'}, {'name': '六出花'}, {'name': '澳梅'}],
            'foliage': [{'name': '银叶菊'}],
            'color_scheme': ['香槟', '浅紫'],
            'packaging': '礼盒花',
        },
    }


def test_llm_merge_keeps_price_in_sync_with_breakdown() -> None:
    """LLM 换过花材重算明细后，顶层价格必须跟着走（回归实测的分叉）。"""
    from agent.tools import _merge_plan

    baseline = _build_plan({'recipient': '恋人', 'occasion': '生日', 'budget': '300'})
    merged = _merge_plan(baseline, _llm_plan_with_pricier_flowers())

    total = merged['budget_breakdown']['total_estimate']
    assert merged['price'] == total, (
        f"价格与明细分叉：price={merged['price']}、明细合计={total}")
    assert _breakdown_sum(merged) == merged['price'], '明细求和与合计对不上'
    assert merged['price_text'] == merged['estimated_price']


def test_enrich_fees_syncs_price() -> None:
    """单测收口点本身：_enrich_plan_fees 重算明细后必须写回价格。"""
    from agent.tools import _enrich_plan_fees

    p = _build_plan({'recipient': '恋人', 'occasion': '生日', 'budget': '300'})
    p['design'].update(_llm_plan_with_pricier_flowers()['design'])
    p['price'] = 1  # 人为制造"旧值"（模拟 baseline 残留）
    out = _enrich_plan_fees(p)
    assert out['price'] == out['budget_breakdown']['total_estimate']
    assert out['price'] != 1, '重算后价格没有收口，仍停在旧值'


def test_llm_cannot_override_deterministic_price_fields() -> None:
    """价格与档位是**系统算的**，模型不得改写。

    模型曾按用户预算写出与实际用料不符的价（用户说 300、它写 317），
    而真实明细是另一个数 —— 三者打架时用户只会认为系统不靠谱。
    """
    from agent.tools import _merge_plan

    baseline = _build_plan({'recipient': '母亲', 'occasion': '祝寿', 'budget': '300'})
    merged = _merge_plan(baseline, {
        'estimated_price': '约 8888 元（顶级档）',
        'budget_tier': '顶级档',
        **_llm_plan_with_pricier_flowers(),
    })
    assert '8888' not in merged['estimated_price'], '模型改写了展示价格'
    assert merged['estimated_price'] == merged['price_text']
    assert merged['budget_tier'] != '顶级档', '模型改写了预算档位'
    assert merged['price'] == merged['budget_breakdown']['total_estimate']


def test_copy_text_total_matches_breakdown_after_merge() -> None:
    """端到端一致性：清单里写的合计，必须等于清单里逐条明细的和。

    这条是用户视角的最终防线 —— 清单是拿给店家看的，账对不上当场露馅。
    """
    from agent.tools import _merge_plan, annotate_shop_materials, build_plan_copy_text

    baseline = _build_plan({'recipient': '恋人', 'occasion': '生日', 'budget': '300'})
    merged = annotate_shop_materials(
        _merge_plan(baseline, _llm_plan_with_pricier_flowers()), '')
    text = build_plan_copy_text(merged)

    lines = [ln for ln in text.split('\n') if ln.startswith('· ') and '——' in ln]
    listed = sum(int(ln.rsplit('——', 1)[1].strip().split(' ')[0]) for ln in lines)
    assert listed == merged['price'], (
        f'清单自身对不上账：明细 {listed} 元、合计 {merged["price"]} 元')
    assert f"合计约 {merged['price']} 元" in text
