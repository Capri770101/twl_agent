"""官网演示页适配层回归门：契约字段、色值表、定价口径。

这些断言对应页面渲染层的硬要求（读 `main.js` 得出）与对外报价口径，
改动适配层时被打回，说明有东西会直接显示错：
- 色板缺 `hex` → 页面色块变成透明；
- 花名自带支数 → 页面显示「康乃馨 ×11 ×11」；
- 页面报价与方案总价不一致 → 用户在两处看到两个价。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.client_payload import (
    build_client_payload,
    build_flowers,
    color_map,
    compute_pricing,
    design_plan,
    material_cost,
    page_tier,
    resolve_palette,
    total_stems,
)


# ── 色值表 ──

def test_color_map_loaded():
    cmap = color_map()
    assert len(cmap) >= 40
    for name in ('粉', '红', '香槟', '银灰', '浅紫', '橄榄绿'):
        assert cmap.get(name, '').startswith('#'), f'{name} 缺色值'


def test_palette_requires_hex():
    pal = resolve_palette(['粉', '香槟'])
    assert [p['name'] for p in pal] == ['粉', '香槟']
    assert all(p['hex'].startswith('#') for p in pal)


def test_palette_drops_unknown_and_dedupes():
    pal = resolve_palette(['不存在色', '红', '正红', '粉'])
    # 未收录的丢弃；红/正红 同色值只留一个
    assert [p['name'] for p in pal] == ['红', '粉']


def test_palette_default_when_nothing_matches():
    pal = resolve_palette(['喵喵色'])
    assert len(pal) == 1 and pal[0]['hex'].startswith('#')


def test_palette_accepts_string():
    assert [p['name'] for p in resolve_palette('粉/香槟')][:2] == ['粉', '香槟']


# ── 档位按售价归属（避免「标准优选 · 380 元」这种自相矛盾展示）──

def test_page_tier_boundaries():
    assert page_tier(150)[0] == '简约心意'
    assert page_tier(151)[0] == '标准优选'
    assert page_tier(300)[0] == '标准优选'
    assert page_tier(301)[0] == '品质甄选'
    assert page_tier(900)[0] == '尊享定制'
    assert page_tier(901)[0] == '高定礼遇'
    assert page_tier(None)[0] == '简约心意'


# ── 成本口径 ──

def test_material_cost_and_stems():
    plan = design_plan('送给妈妈一束花，预算200')
    design = plan['design']
    assert total_stems(design) > 0
    # material_cost 是 int() 截断，且单价现在是小数（如 4.3）→ 用 int 包一层再比
    assert material_cost(design) == int(sum(
        (f.get('qty') or 0) * (f.get('unit_price') or 0)
        for key in ('main_flowers', 'fillers', 'foliage')
        for f in (design.get(key) or [])
    ))


# ── 定价口径（2026-09-18 变更）────────────────────────────────────────────
# `unit_price` 已从「代码里写死的成本参考价」改为「平台在售零售价」（agent/pricing）。
# 所以**不再对材料额乘 MIN_MARGIN** —— 那等于对零售价二次加价，实测会把一束
# 195 元的方案抬到 201 元、直接顶破用户预算。
# 保底含义改为：报价不低于花材自身零售额（不把花材打折卖）。

PRICING_CASES = (
    '送给妈妈一束花，预算100',
    '送给妈妈一束花，预算200',
    '女朋友生日花束，预算500',
    '开业花篮 预算1000',
    '随便来一束好看的花',
)


def test_pricing_never_below_material_cost():
    """保底：报价不得低于花材零售合计。"""
    for text in PRICING_CASES:
        pricing = compute_pricing(design_plan(text))
        assert pricing['suggested'] >= pricing['materialCost'], text
        assert pricing['stems'] > 0, text
        assert pricing['margin'] == pricing['suggested'] - pricing['materialCost'], text


def test_pricing_matches_plan_total():
    """页面报价必须与方案总价严格一致 —— 两处口径分叉会让用户看到两个价。"""
    for text in PRICING_CASES:
        plan = design_plan(text)
        pricing = compute_pricing(plan)
        assert pricing['suggested'] == plan['budget_breakdown']['total_estimate'], text


def test_pricing_warns_when_budget_too_low():
    """预算远低于配置成本 → 必须给出 warning（页面会用强调色渲染）。"""
    payload = build_client_payload('送女朋友99朵红玫瑰，预算100')
    pricing = payload['plan']['pricing']
    assert pricing['withinBudget'] is False
    assert pricing['warning']


# ── 启发式枝数让位预算（否则 200 元预算会产出 300+ 元方案）──

def test_heuristic_bunch_count_yields_to_budget():
    """「一束」是量词不是枝数：给了预算就该由预算决定配置。"""
    payload = build_client_payload('送给妈妈一束花，预算200', {'recipient': '妈妈', 'budget': 200})
    pricing = payload['plan']['pricing']
    assert pricing['suggested'] <= 240, pricing          # 贴着预算，而不是 318 元
    assert pricing['withinBudget'] is True


def test_explicit_stem_count_is_kept():
    """显式枝数（「11朵」）必须保留，不能被预算改少。"""
    plan = design_plan('11朵粉玫瑰，送给女朋友，预算200')
    assert total_stems(plan['design']) >= 11


# ── 页面契约 ──

def _payload(text='送给妈妈一束花，预算200', parsed=None):
    return build_client_payload(text, parsed if parsed is not None else {'recipient': '妈妈', 'budget': 200})


def test_payload_contract_fields():
    payload = _payload()
    assert payload['success'] is True and payload['mode'] == 'live'
    plan = payload['plan']
    for key in ('title', 'tier', 'flowers', 'palette', 'language', 'craft', 'pricing', 'image', 'note'):
        assert key in plan, key
    for key in ('suggested', 'materialCost', 'margin', 'marginRate', 'stems', 'withinBudget', 'warning', 'reason'):
        assert key in plan['pricing'], key
    assert payload['parsed']['recipient']


def test_flowers_name_has_no_count():
    """页面渲染成 `name ×count`，花名自带支数会变成「康乃馨 ×11 ×11」。"""
    for text in ('送给妈妈一束花', '19朵红玫瑰花束', '开业花篮'):
        payload = build_client_payload(text, {})
        assert payload['plan']['flowers']
        for f in payload['plan']['flowers']:
            assert '×' not in f['name'] and 'x' not in f['name'].lower(), (text, f)


def test_flowers_have_meaning_from_kb():
    flowers = build_flowers(design_plan('送给妈妈一束花')['design'])
    assert any(f['meaning'] for f in flowers), '花语应来自知识库'


def test_note_placed_inside_plan():
    """页面只读 plan.note（pl.note || pr.reason），顶层 note 不会被渲染。"""
    payload = build_client_payload('随便来一束花', {})
    assert payload['plan']['note'], '信息不足时应给出说明'
    assert '送给谁' in payload['plan']['note']


def test_image_is_none_for_now():
    assert _payload()['plan']['image'] is None


def test_parsed_budget_is_echoed():
    payload = _payload(parsed={'budget': 500})
    assert payload['parsed']['budget'] == 500
