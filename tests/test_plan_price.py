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
