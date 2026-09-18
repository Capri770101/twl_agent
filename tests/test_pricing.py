"""花材价格表（`agent/pricing`）的回归测试。

背景（Capri 2026-09-18 拍板）：DIY 定价从「代码里写死的三条常数」改为
**从平台在售商品反推** —— 有确定店铺时按该店商品拟合该店专属价，没锁店时用全网实测基线。

这里锁住 5 组事：
1. **配方解析**：`flowers` 文本 → `[(花材, 支数)]`（含去重、多花同框平分、未知花材忽略）；
2. **拟合正确性**：构造已知参数的样本 → 必须解回同样的参数；
3. **分级兜底**：该店实测 → 全网基线，任何一步失败都不能抛错（报价是主流程）；
4. **取数失败也被缓存**：否则每轮对话都会去打一次平台接口；
5. **永不返回 0 单价**：报价里出现 0 元/支 = 白送。
"""

from __future__ import annotations

import pytest

from agent import pricing
from agent.pricing import (
    PriceTable,
    baseline_table,
    fit_price_table,
    parse_recipe,
    price_table,
)

BASE = 35.0
UNIT_ROSE = 4.3
UNIT_CARNATION = 2.6


@pytest.fixture(autouse=True)
def _clean_cache():
    pricing.clear_cache()
    yield
    pricing.clear_cache()


def _rows_from(recipe_list: list[tuple[float, list[tuple[str, int]]]]) -> list[dict]:
    """把 ``[(售价元, [(花材, 支数)])]`` 转成平台商品行的样子。

    ⚠️ ``price`` 按**元**填 —— 传入本模块的行是 ``normalize_row`` 规范化后的，
    其价格已经由「分」换算成「元」（见 ``agent.pricing._price_yuan``）。
    """
    rows = []
    for price, recipe in recipe_list:
        text = ''.join(f'{qty}枝{name}' for name, qty in recipe)
        rows.append({'price': round(price, 2), 'flowers': [text]})
    return rows


def _known_samples() -> list[tuple[float, list[tuple[str, int]]]]:
    """参数已知的样本（**数量必须 ≥ pricing._MIN_SAMPLES**）：验证「拟合能解回原参数」。"""
    out: list[tuple[float, list[tuple[str, int]]]] = []
    for qty in (5, 8, 11, 16, 19, 24, 33, 44, 52, 66):
        out.append((BASE + UNIT_ROSE * qty, [('玫瑰', qty)]))
    for qty in (11, 16, 26, 33, 44):
        out.append((46.0 + UNIT_CARNATION * qty, [('康乃馨', qty)]))
    for qty in (10, 15, 20, 25, 30):
        out.append((BASE + UNIT_ROSE * qty + UNIT_CARNATION * 5,
                    [('玫瑰', qty), ('康乃馨', 5)]))
    assert len(out) >= pricing._MIN_SAMPLES
    return out


# ── 1. 配方解析 ──────────────────────────────────────────────────────────

def test_parse_single_flower() -> None:
    assert parse_recipe('11枝粉玫瑰花混搭') == [('玫瑰', 11)]


def test_parse_multi_flower_keeps_each() -> None:
    got = parse_recipe('11枝香槟玫瑰4朵向日葵6枝碎冰蓝混搭花束')
    assert ('玫瑰', 11) in got and ('向日葵', 4) in got
    # 「碎冰蓝」不在知识库里 → 忽略（宁可少算一种，也不猜错价格）
    assert all(name in ('玫瑰', '向日葵') for name, _ in got)


def test_parse_splits_quantity_across_flowers() -> None:
    """「9朵玫瑰蝴蝶兰」这种一个数量跟两种花 → 平分，不偏袒任一。"""
    got = dict(parse_recipe('9朵玫瑰蝴蝶兰混搭花束'))
    assert got.get('玫瑰') == 4 and got.get('蝴蝶兰') == 4


def test_parse_dedupes_repeated_pair() -> None:
    """同一 (花材, 支数) 重复出现只算一次 —— 上游文本重复会让支数翻倍。"""
    assert parse_recipe('11枝玫瑰 11枝玫瑰混搭') == [('玫瑰', 11)]


@pytest.mark.parametrize('bad', ['', '香槟玫瑰洋桔梗混搭花束', None])
def test_parse_returns_empty_without_quantity(bad) -> None:
    """没有「数字+量词」就没有支数信息 → 返回空（不能瞎猜 1 支）。"""
    assert parse_recipe(bad or '') == []


# ── 2. 拟合正确性 ────────────────────────────────────────────────────────

def test_fit_recovers_known_parameters() -> None:
    """构造已知参数 → 必须解回原参数（这是整个定价的地基）。"""
    table = fit_price_table(_known_samples(), 's_test')
    assert table is not None
    assert table.unit_prices['玫瑰'] == pytest.approx(UNIT_ROSE, abs=0.3)
    assert table.unit_prices['康乃馨'] == pytest.approx(UNIT_CARNATION, abs=0.3)
    # 两组纯花材样本的基础费分别是 35（玫瑰）与 46（康乃馨），拟合取中位 → 落在两者之间
    assert 35 <= table.base_fee <= 46


def test_fit_needs_enough_samples() -> None:
    """样本不足必须返回 None（交给调用方降级）—— 少样本会解出噪声系数。"""
    assert fit_price_table(_known_samples()[:3], 's_x') is None


def test_rows_use_yuan_not_cents() -> None:
    """传入的行价格单位是**元**（`normalize_row` 已换算过）。

    踩过的真实坑：这里多除了一次 100 → 全表价格缩小 100 倍 → 拟合单价低于下限 →
    **每个店铺都静默降级到全网基线**，表现是「锁店专属定价怎么都不生效」，极难排查。
    """
    rows = _rows_from(_known_samples())
    table = pricing.fit_from_rows(rows, 's_test')
    assert table is not None
    assert table.samples == len(_known_samples())
    assert table.unit_prices['玫瑰'] == pytest.approx(UNIT_ROSE, abs=0.3)
    assert table.base_fee > 10, '基础费按元口径应落在几十元'


def test_absurdly_low_prices_rejected_with_unit_hint(caplog: pytest.LogCaptureFixture) -> None:
    """价格中位数低到不可能（< 5 元）→ 放弃拟合并留下 error 日志。

    这正是「多除一次 100」的现场表现，必须有明确留痕，否则只能靠猜。
    """
    rows = [{'price': round(p / 100, 3),
             'flowers': [''.join(f'{q}枝{n}' for n, q in rec)]}
            for p, rec in _known_samples()]
    with caplog.at_level('ERROR', logger='agent.pricing'):
        assert pricing.fit_from_rows(rows, 's_bad_unit') is None
    assert any('单位错误' in r.message or '单位错误' in r.getMessage() for r in caplog.records)


def test_fit_rejects_out_of_range_unit_price() -> None:
    """个别样本异常不能产出离谱单价（单价被钳在上限内）。"""
    samples = list(_known_samples())
    samples.append((99999.0, [('玫瑰', 1)]))       # 明显异常的商品
    table = fit_price_table(samples, 's_x')
    if table is not None:
        for name, value in table.unit_prices.items():
            assert 0 < value <= pricing._MAX_UNIT, f'{name}={value}'


# ── 3. 分级兜底 ──────────────────────────────────────────────────────────

def test_no_shop_uses_baseline() -> None:
    table = price_table('')
    assert table.source == 'baseline'
    assert table.base_fee > 0


def test_fetch_failure_falls_back_to_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """取数失败**不能抛错** —— 报价是主流程，宁可降级也不能崩。"""
    monkeypatch.setattr(pricing, '_fetch_rows', lambda sid: None)
    assert price_table('s_unreachable').source == 'baseline'


def test_too_few_rows_falls_back_to_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, '_fetch_rows',
                        lambda sid: _rows_from(_known_samples()[:2]))
    assert price_table('s_thin').source == 'baseline'


def test_merchant_table_used_when_enough_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricing, '_fetch_rows',
                        lambda sid: _rows_from(_known_samples()))
    table = price_table('s_good')
    assert table.source == 'merchant'
    assert table.shop_id == 's_good'
    assert table.samples >= pricing._MIN_SAMPLES


def test_failed_fetch_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """取数失败也要缓存 —— 否则每轮对话都会去打一次平台接口。"""
    calls: list[str] = []

    def _fake(sid: str):
        calls.append(sid)
        return None

    monkeypatch.setattr(pricing, '_fetch_rows', _fake)
    price_table('s1')
    price_table('s1')
    assert len(calls) == 1


# ── 4. 取值与报价 ────────────────────────────────────────────────────────

def test_unit_price_never_zero_for_unknown_flower() -> None:
    """表里没有的花材也要给价 —— 返回 0 等于白送。"""
    table = baseline_table()
    value = table.unit_price('这不是一种花')
    assert value > 0
    assert value == pricing._UNIT_BY_TIER[pricing._tier_of('这不是一种花')]


def test_unknown_flower_tier_fallback_values() -> None:
    """档位兜底值必须都为正且量级合理（元/支）。"""
    for tier, value in pricing._UNIT_BY_TIER.items():
        assert 0 < value < pricing._MAX_UNIT, tier


def test_price_of_is_two_part_model() -> None:
    """总价 = 基础费 + Σ(支数 × 单价) —— 这是与平台一致的两段式结构。"""
    table = PriceTable(base_fee=40.0, unit_prices={'玫瑰': 4.3}, source='baseline')
    assert table.price_of([('玫瑰', 11)]) == round(40 + 11 * 4.3)
    assert table.price_of([]) == 40
    assert table.price_of([('玫瑰', 0)]) == 40


def test_price_table_is_immutable() -> None:
    """价表是 frozen dataclass —— 防止被就地改坏后影响其它调用方。"""
    table = baseline_table()
    with pytest.raises(Exception):
        table.base_fee = 1.0  # type: ignore[misc]


def test_baseline_matches_documented_market_values() -> None:
    """基线值不能悄悄漂走：它必须仍在平台实测的量级（玫瑰 ~4 元/支，中位价 108 元）。"""
    table = baseline_table()
    assert 3.0 <= table.unit_prices['玫瑰'] <= 6.0
    assert table.unit_prices['康乃馨'] < table.unit_prices['玫瑰']
    assert table.unit_prices['绣球'] > table.unit_prices['玫瑰']
    price_11 = table.price_of([('玫瑰', 11)])
    assert 60 <= price_11 <= 120, f'11 支玫瑰报价 {price_11} 元，偏离平台实测（88 元）'
