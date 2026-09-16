"""商品卡与回复文案对齐回归门（2026-09-16 线上缺陷）。

平台 ``products`` 里同款花束会在多家店铺**各自上架**，``platform_db_query_entity``
一次返回 20 行原始数据 → 演示页出现「文案推荐 4 款、卡片区却把同一款 ¥158 重复 6 遍」。

这里保护 ``ReActAgent._align_products_with_reply``（纯函数，不连 LLM / DB / 网络）。
"""
from __future__ import annotations

from agent.agent import ReActAgent, UIType, _align_card_data_with_reply

_align = ReActAgent._align_products_with_reply


def _rows(*pairs: tuple[str, int]) -> list[dict]:
    return [{'name': n, 'price': p} for n, p in pairs]


def test_dedupes_same_name_and_price():
    rows = _rows(('感恩母亲', 158), ('感恩母亲', 158), ('感恩母亲', 158), ('春风暖阳', 168))
    assert [r['name'] for r in _align(rows, '')] == ['感恩母亲', '春风暖阳']


def test_same_name_different_price_collapses_to_one():
    """同款在不同店铺定价会不同（实测「粉色梦境」s004=118 / s008=129）——
    同名即同款，只保留平台排序靠前的那条。"""
    rows = _rows(('粉色梦境', 118), ('粉色梦境', 129), ('幸运女神', 118))
    out = _align(rows, '')
    assert [r['name'] for r in out] == ['粉色梦境', '幸运女神']
    assert out[0]['price'] == 118


def test_filters_to_products_mentioned_in_reply():
    rows = _rows(('感恩母亲', 158), ('春风暖阳', 168), ('温柔以待', 146))
    out = _align(rows, '给你挑了 感恩母亲 和 温柔以待 这两款')
    assert [r['name'] for r in out] == ['感恩母亲', '温柔以待']


def test_falls_back_to_all_when_reply_matches_nothing():
    """模型换说法/用别名时不能把卡片清空——宁多勿漏。"""
    rows = _rows(('感恩母亲', 158), ('春风暖阳', 168))
    assert len(_align(rows, '这几款都挺适合送妈妈')) == 2


def test_caps_number_of_cards():
    rows = _rows(*[(f'花束{i}', 100 + i) for i in range(30)])
    assert len(_align(rows, '')) == ReActAgent.PRODUCT_CARD_LIMIT


def test_ignores_malformed_rows():
    rows = [{'name': '', 'price': 1}, 'not-a-dict', {'price': 9}, {'name': '玫瑰', 'price': 99}]
    assert [r['name'] for r in _align(rows, '')] == ['玫瑰']


def test_empty_input_returns_empty():
    assert _align([], '') == []
    assert _align(None, '随便看看') == []


# ── _align_card_data_with_reply（最终回复定型后再对齐） ──────────────

def test_card_data_keeps_diy_and_filters_products():
    """混合卡：DIY 方案原样保留，只过滤商品。"""
    diy = {'name': '定制方案', 'diy': True, 'design': {'main_flowers': []}}
    data = {'plans': [diy, {'name': '感恩母亲', 'price': 158}, {'name': '春风暖阳', 'price': 168}]}
    out = _align_card_data_with_reply(UIType.PLAN_CARD, data, '推荐 春风暖阳 这一款')
    assert [p.get('name') for p in out['plans']] == ['定制方案', '春风暖阳']
    # 不改原对象（避免污染上游 data）
    assert len(data['plans']) == 3


def test_card_data_untouched_for_non_plan_ui():
    data = {'plans': [{'name': 'A', 'price': 1}]}
    assert _align_card_data_with_reply(UIType.SHOP_CARD, data, 'A') is data


def test_card_data_dedupes_even_without_reply():
    """没有回复可对齐时也要去重——重复卡片本身就是问题。"""
    data = {'plans': [{'name': 'A', 'price': 1}, {'name': 'A', 'price': 1}]}
    out = _align_card_data_with_reply(UIType.PLAN_CARD, data, '')
    assert [p['name'] for p in out['plans']] == ['A']


def test_card_data_untouched_when_nothing_to_change():
    data = {'plans': [{'name': 'A', 'price': 1}, {'name': 'B', 'price': 2}]}
    assert _align_card_data_with_reply(UIType.PLAN_CARD, data, '') is data


def test_card_data_untouched_for_pure_diy_card():
    data = {'plans': [{'name': '定制方案', 'diy': True, 'design': {}}]}
    assert _align_card_data_with_reply(UIType.PLAN_CARD, data, '定制方案') is data

