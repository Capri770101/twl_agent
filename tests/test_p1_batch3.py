"""第三批 P1 回归门（2026-09-18）：预算数量级 / 商品卡主名匹配 / 色系逐级放宽。

三项都是外部安全审计 P1 里「本仓库可修」的部分，根因与判据如下：

1. **「预算5万」被解析成 5 元** —— 旧正则 `\\d{1,5}` 只抓数字、把「万」丢掉；
   「1.5万」更是只剩 1。金额差 4 个数量级 → 方案档位与报价全错。
2. **「文案说 5 款、卡片只渲染 4 张」** —— 平台商品名是「主名·副名」结构
   （「感恩母亲·康乃馨花束」），模型写文案惯用简称（「感恩母亲」），
   只认全名会把该款误剔除。
3. **「色系检索不过滤」** —— 用户/模型说「粉色系」「香槟色」，商品文案写「粉色」「香槟」，
   整词匹配零命中 → 放宽成全量（连「随机花瓶一个」这类无关商品都返回）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import ReActAgent
from agent.tools import extract_requirement, merge_requirement
from backend.data_gateway import http_source as hs


# ══════════════════════════════════════════════════════════════════════
# 1. 预算数量级
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('text, expect', [
    ('预算5万', 50000),
    ('预算5万元', 50000),
    ('预算1.5万', 15000),
    ('预算三万', 30000),
    ('预算五千', 5000),
    ('预算两百', 200),
    ('预算 3000 左右', 3000),
    ('三千块', 3000),
    ('三百元的预算', 300),
    ('5w预算', 50000),
    ('五千元左右', 5000),
    ('预算200', 200),
])
def test_budget_magnitude_units(text: str, expect: float) -> None:
    assert extract_requirement(text).budget_num == expect


@pytest.mark.parametrize('text', [
    '11朵玫瑰',              # 支数不是预算
    '两万朵玫瑰的花束',        # 「万」+「朵」= 数量，不是金额
    '想买束花',              # 完全没有价格信号
    '三十支康乃馨',
])
def test_budget_not_falsely_extracted(text: str) -> None:
    assert extract_requirement(text).budget_num is None, f'{text!r} 不该被当成预算'


def test_budget_range_follows_magnitude() -> None:
    """区间 ±20% 要跟着数量级走，不能被截断。"""
    r = extract_requirement('预算5万')
    assert (r.budget_min, r.budget_max) == (40000, 60000)


def test_merge_budget_accepts_wan() -> None:
    """模型侧写的「约5万」也要能解析（旧实现只认 2~5 位纯数字）。"""
    req = extract_requirement('想买束花')
    assert merge_requirement(req, {'budget': '约5万'}).budget_num == 50000
    assert merge_requirement(req, {'budget': '1.5万'}).budget_num == 15000


# ══════════════════════════════════════════════════════════════════════
# 2. 商品卡与文案对齐（主名匹配）
# ══════════════════════════════════════════════════════════════════════

ROWS = [
    {'name': '感恩母亲·康乃馨花束', 'price': 158},
    {'name': '春风暖阳', 'price': 168},
    {'name': '温柔以待', 'price': 146},
    {'name': '幸运女神', 'price': 118},
    {'name': '蜜语春晖', 'price': 68},
]


def test_reply_with_short_names_keeps_all_cards() -> None:
    """回归本次事故：文案用简称时，全名款不该被剔除。"""
    reply = ('帮你挑了 5 款：1. 感恩母亲 158 元；2. 春风暖阳 168 元；3. 温柔以待 146 元；'
             '4. 幸运女神 118 元；5. 蜜语春晖 68 元')
    out = ReActAgent._align_products_with_reply(ROWS, reply)
    assert len(out) == 5, f'文案说 5 款、卡片只出 {len(out)} 张'
    assert '感恩母亲·康乃馨花束' in [r['name'] for r in out]


@pytest.mark.parametrize('sep', ['·', '-', '|', '｜'])
def test_name_separator_variants(sep: str) -> None:
    assert ReActAgent._name_mentioned(f'感恩母亲{sep}康乃馨花束', '推荐感恩母亲')


def test_single_char_main_name_not_matched() -> None:
    """主名只有一个字时不认 —— 否则「爱」会命中一大片。"""
    assert ReActAgent._name_mentioned('爱', '随便写点什么') is False
    assert ReActAgent._name_mentioned('爱·红玫瑰', '这个字是爱') is False


def test_full_name_still_works() -> None:
    assert ReActAgent._name_mentioned('春风暖阳', '推荐春风暖阳')
    assert ReActAgent._name_mentioned('春风暖阳', '春风暖阳很不错')


def test_no_mention_falls_back_to_all() -> None:
    """一个都没点名 → 回退全集（宁多勿漏，卡片不能空）。"""
    assert len(ReActAgent._align_products_with_reply(ROWS, '随便看看')) == 5


def test_partial_mention_keeps_only_those() -> None:
    """只点名 2 款就只留 2 张（模型已替用户筛过）。"""
    out = ReActAgent._align_products_with_reply(ROWS, '推荐春风暖阳和温柔以待')
    assert [r['name'] for r in out] == ['春风暖阳', '温柔以待']


def test_empty_reply_keeps_all() -> None:
    assert len(ReActAgent._align_products_with_reply(ROWS, '')) == 5


# ══════════════════════════════════════════════════════════════════════
# 3. 色系逐级放宽
# ══════════════════════════════════════════════════════════════════════

def test_token_levels_expansion() -> None:
    assert hs._token_levels(['粉色系']) == [['粉色系'], ['粉色'], ['粉']]
    assert hs._token_levels(['香槟色']) == [['香槟色'], ['香槟']]
    assert hs._token_levels(['白绿色系']) == [['白绿色系'], ['白绿色'], ['白绿'], ['白', '绿']]


def test_non_color_word_not_split() -> None:
    """普通词不拆分（「玫瑰」不能变成「玫」「瑰」）。"""
    assert hs._token_levels(['玫瑰']) == [['玫瑰']]
    assert hs._token_levels(['康乃馨']) == [['康乃馨']]


COLOR_ROWS = [
    {'id': 'p1', 'name': '粉色梦境', 'price': 11800, 'ownerShopId': 's001'},
    {'id': 'p2', 'name': '香槟玫瑰礼盒', 'price': 12800, 'ownerShopId': 's001'},
    {'id': 'p3', 'name': '白色百合花束', 'price': 8800, 'ownerShopId': 's001', 'description': '配绿叶'},
    {'id': 'p4', 'name': '红色热恋', 'price': 9800, 'ownerShopId': 's001'},
]


def _payload(rows):
    return {'code': 0, 'data': {'list': rows}}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('PLATFORM_API_AISTORE_URL', 'https://example.com')
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(COLOR_ROWS))
    hs.clear_cache()
    yield 'aistore'
    hs.clear_cache()


@pytest.mark.parametrize('keyword, expect_ids', [
    ('粉色系', ['p1']),
    ('香槟色', ['p2']),
    ('白绿色系', ['p3']),      # 靠末级拆单字命中（「白」+「绿」）
])
def test_color_query_is_filtered(configured, keyword: str, expect_ids: list[str]) -> None:
    """色系查询必须真的过滤 —— 这是审计「色系检索不过滤」的回归门。"""
    meta: dict = {}
    out = hs.fetch_entity('aistore', 'plan', keyword=keyword, meta=meta)
    assert [r['id'] for r in out] == expect_ids, f'{keyword!r} 返回了不相关商品：{[r["id"] for r in out]}'
    assert meta['match'] == 'partial', '放宽过词形，应如实标注 partial'


def test_literal_color_query_is_exact(configured) -> None:
    """字面就命中的色词（「红色」）标 exact —— 没有放宽过，不该误报 partial。"""
    meta: dict = {}
    out = hs.fetch_entity('aistore', 'plan', keyword='红色', meta=meta)
    assert [r['id'] for r in out] == ['p4']
    assert meta['match'] == 'exact'


def test_color_relax_marks_partial_not_exact(configured) -> None:
    """放宽过的结果标 partial（模型据此措辞），全字面命中才标 exact。"""
    meta: dict = {}
    hs.fetch_entity('aistore', 'plan', keyword='粉色', meta=meta)
    assert meta['match'] == 'exact'


def test_color_relax_still_respects_shop(configured) -> None:
    """词形放宽不得越过硬约束。"""
    meta: dict = {}
    assert hs.fetch_entity('aistore', 'plan', keyword='粉色系', shop_id='s999') == []


# ══════════════════════════════════════════════════════════════════════
# 4. DIY 步骤条数上限（审计提到「制作步骤 146 步」）
# ══════════════════════════════════════════════════════════════════════

def test_cap_steps_truncates_long_list() -> None:
    """146 步这类不可控输出必须钳制 —— 规则引擎固定 6 步，同一量级。"""
    from agent.tools import _DIY_STEPS_MAX, _cap_steps

    assert _DIY_STEPS_MAX <= 12, '上限不该大到失去意义'
    assert len(_cap_steps([f'步骤{i}' for i in range(146)])) == _DIY_STEPS_MAX


def test_cap_steps_keeps_short_list_intact() -> None:
    from agent.tools import _cap_steps

    steps = [f'步骤{i}' for i in range(6)]
    assert _cap_steps(steps) == steps


def test_cap_steps_cleans_non_list_and_blank() -> None:
    from agent.tools import _cap_steps

    assert _cap_steps('不是列表') == []
    assert _cap_steps(None) == []
    assert _cap_steps(['  ', 'A', '', 'B']) == ['A', 'B']


def test_rule_engine_steps_within_cap() -> None:
    """规则引擎的步骤本身就该在合理范围（它是模型输出的参照基准）。"""
    from agent.tools import _DIY_STEPS_MAX, _build_plan

    plan = _build_plan({'recipient': '母亲', 'occasion': '祝寿', 'budget': '300'})
    assert 0 < len(plan.get('diy_steps') or []) <= _DIY_STEPS_MAX
