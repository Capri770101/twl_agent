"""商品卡与回复文案对齐回归门（2026-09-16 线上缺陷 + 2026-09-20 生产实测补充）。

平台 ``products`` 里同款花束会在多家店铺**各自上架**，``platform_db_query_entity``
一次返回 20 行原始数据 → 演示页出现「文案推荐 4 款、卡片区却把同一款 ¥158 重复 6 遍」。

2026-09-20 生产实测又暴露两条**反方向**的不一致（本次一并锁住）：
  · 模型分两轮换关键词查商品，文案把两轮结果都点了名，而卡片只取**最后一次**查询 →
    「文案说 4 款、卡片只渲染 2 款」；→ ``_collect_plan_rows`` 取**并集**；
  · 文案自报的数字是模型**猜的**（工具契约要求「先写 reply、再定卡片」）→
    文案说 4 款却只给 2 张 → ``_align_reply_count_with_card`` 以卡片为准回写数字。

这里保护 ``_align_products_with_reply`` / ``_align_card_data_with_reply`` /
``_collect_plan_rows`` / ``_align_reply_count_with_card``（纯函数，不连 LLM / DB / 网络）。
"""
from __future__ import annotations

import json

from agent.agent import (
    ReActAgent,
    UIType,
    _align_card_data_with_reply,
    _align_reply_count_with_card,
    _collect_plan_rows,
)

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


def test_mixed_card_drops_products_not_mentioned():
    """⚠️ 2026-09-20：混合卡（DIY + 商品）里，文案**一个商品名都没点名**时不要塞商品。

    纯商品卡「宁多勿漏」是为了避免卡片空掉；混合卡已有 DIY 方案撑着，
    再倒一串文案没提过的商品 = **反向不一致**（卡片里有、文案里没提）。"""
    diy = {'name': '定制方案', 'diy': True, 'design': {}}
    data = {'plans': [diy, {'name': '感恩母亲', 'price': 158}, {'name': '春风暖阳', 'price': 168}]}
    out = _align_card_data_with_reply(UIType.PLAN_CARD, data, '给你配好了，花材和价格都在卡片里')
    assert [p.get('name') for p in out['plans']] == ['定制方案']


# ── _collect_plan_rows（本轮所有商品查询的并集）──────────────────────

class _Query:
    """最小工具调用记录替身：只带 ``_collect_plan_rows`` 需要的四个字段。"""

    def __init__(self, rows=None, entity: str = 'plan', ok: bool = True,
                 status: str = 'ok', raw: str | None = None) -> None:
        self.name = 'platform_db_query_entity'
        self.status = status
        self.arguments = {'entity': entity, 'keyword': 'kw'}
        self.result = raw if raw is not None else json.dumps({'ok': ok, 'data': rows or []})


def test_collect_plan_rows_unions_all_query_rounds():
    """⚠️ 2026-09-20 生产回归：模型分两轮换关键词查商品，卡片必须取**并集**。

    原实现只取最后一次查询的结果 → 文案点名了第一轮查到的商品，卡片里却没有它，
    表现为「文案说 4 款、卡片只渲染 2 款」。"""
    log = [_Query(rows=[{'name': '月光漫步'}]), _Query(rows=[{'name': '网红Kitty'}])]
    assert [r['name'] for r in _collect_plan_rows(log)] == ['月光漫步', '网红Kitty']


def test_collect_plan_rows_skips_failed_and_other_entities():
    log = [
        _Query(rows=[{'name': 'A'}]),
        _Query(rows=[{'name': 'B'}], ok=False),          # 业务失败（ok 不为 True）
        _Query(rows=[{'name': 'C'}], status='error'),    # 工具异常
        _Query(rows=[{'name': 'D'}], entity='shop'),     # 店铺查询不算商品
    ]
    assert [r['name'] for r in _collect_plan_rows(log)] == ['A']


def test_collect_plan_rows_ignores_malformed():
    log = [_Query(raw='not json'), _Query(rows=[{'name': 'B'}, 'oops']), _Query(raw='')]
    assert [r['name'] for r in _collect_plan_rows(log)] == ['B']


def test_collect_plan_rows_empty():
    assert _collect_plan_rows([]) == []
    assert _collect_plan_rows(None) == []


# ── _align_reply_count_with_card（数量回写）──────────────────────────

def test_reply_count_rewritten_to_card_count():
    """⚠️ 2026-09-20 生产回归：文案「我挑了 4 款」配 2 张卡片 → 回写为 2 款。

    模型是先写文案再定卡片的（为压首字延迟），那个数字是猜的；
    卡片是按文案**点名**的商品裁出来的，所以以卡片为准回写数字。"""
    data = {'plans': [{'name': 'A', 'price': 1}, {'name': 'B', 'price': 2}]}
    out = _align_reply_count_with_card(
        '我挑了 4 款风格不同的，都在预算内：A 和 B', UIType.PLAN_CARD, data)
    assert '4 款' not in out
    assert '2 款' in out


def test_reply_count_rewrites_only_first_occurrence():
    """只改第一处（与 ``_reply_declared_count`` 取第一处的口径一致）。"""
    data = {'plans': [{'name': 'A', 'price': 1}]}
    out = _align_reply_count_with_card('我挑了 4 款，另外 3 款也可以备选', UIType.PLAN_CARD, data)
    assert out.startswith('我挑了 1 款')
    assert '另外 3 款' in out


def test_reply_count_untouched_when_consistent():
    data = {'plans': [{'name': 'A'}, {'name': 'B'}]}
    reply = '给你挑了 2 款，都在预算内'
    assert _align_reply_count_with_card(reply, UIType.PLAN_CARD, data) == reply


def test_reply_count_untouched_without_products():
    """纯 DIY 卡没有商品条数可比 → 不动文案。"""
    data = {'plans': [{'name': '定制方案', 'diy': True, 'design': {}}]}
    assert _align_reply_count_with_card('给你挑了 4 款', UIType.PLAN_CARD, data) == '给你挑了 4 款'


def test_reply_count_untouched_for_non_plan_ui():
    data = {'plans': [{'name': 'A'}]}
    assert _align_reply_count_with_card('挑了 4 款', UIType.TEXT, data) == '挑了 4 款'
    assert _align_reply_count_with_card('挑了 4 款', UIType.SHOP_CARD, data) == '挑了 4 款'


def test_reply_count_ignores_stem_quantity():
    """「33 朵玫瑰」是花材数量、不是商品数 → 不该被改写（量词只认 款/束）。"""
    data = {'plans': [{'name': 'A', 'price': 1}]}
    reply = '这是 33 朵玫瑰的高档花束，我挑了 1 款给你'
    assert _align_reply_count_with_card(reply, UIType.PLAN_CARD, data) == reply

