"""DIY 方案的「复制用料清单」文本（Capri 2026-09-18 拍板：先做 DIY 的轻量成交通口）。

背景：平台没有定制 SKU，DIY 方案生成完就断了 —— 用户拿不到任何可执行的东西，
商家也永远看不到需求。在「定制需求单」接口落地（待平台方）之前，
「复制一份清单去找店家沟通」是让 DIY 从体验价值变成商业价值的唯一出口。

这里锁住 4 组事：
1. **内容完整**：对象/场合/配色/包装/用料/费用明细/合计都要在，用户拿去就能对上；
2. **金额一致**：清单合计必须与 ``plan['price']`` 同源，不能"卡片一个价、清单另一个价"；
3. **不误导**：价格标注是**估算**、须声明「以门店报价为准」、须有 AI 生成说明（合规）；
4. **不泄漏**：内部字段名（plan_id / budget_breakdown 等）不得出现在面向店家的文本里。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.tools import (
    _COPY_TEXT_MAX,
    _build_plan,
    annotate_shop_materials,
    build_plan_copy_text,
)

DIMS_CASES = [
    {'recipient': '女朋友', 'occasion': '生日', 'budget': '300'},
    {'recipient': '妈妈', 'occasion': '母亲节'},                       # 未给预算
    {'recipient': '客户', 'occasion': '商务馈赠', 'budget': '800'},     # 高档
]


def _make(dims: dict, shop_id: str = '') -> dict:
    """造一份带 ``copy_text`` 的定型方案（对齐 :func:`design_diy_plan` 的产出）。"""
    p = annotate_shop_materials(_build_plan(dims), shop_id)
    p['copy_text'] = build_plan_copy_text(p)
    return p


@pytest.fixture
def plan() -> dict:
    """一份常规定型方案（未锁店 → 无缺料标注）。"""
    return _make(DIMS_CASES[0])


# ── 1. 内容完整 ──────────────────────────────────────────────────────────

def test_contains_title_and_name(plan: dict) -> None:
    text = plan['copy_text']
    assert text.startswith('【定制花束需求单】')
    assert plan['name'] in text


def test_contains_recipient_occasion_colors_packaging(plan: dict) -> None:
    text = plan['copy_text']
    assert f"对象：{plan['recipient']}" in text
    assert f"场合：{plan['occasion']}" in text
    assert '配色：' in text
    assert '包装：' in text
    assert plan['design']['color_scheme'][0] in text


def test_contains_material_summary(plan: dict) -> None:
    """「用料：玫瑰×5、…」—— 用户扫一眼就知道这束花用什么。"""
    assert '用料：' in plan['copy_text']
    assert plan['design']['fees']['stem_count'] in plan['copy_text']


@pytest.mark.parametrize('dims', DIMS_CASES)
def test_lists_every_nonzero_material_item(dims: dict) -> None:
    """非零花材项必须逐条列出（用户要拿去比价，漏项会配不齐）。"""
    p = _make(dims)
    text = p['copy_text']
    for item in p['budget_breakdown']['items']:
        if item.get('item') in ('人工费', '装饰费') or not item.get('amount'):
            continue
        assert item['item'] in text, f"清单漏了「{item['item']}」"


def test_style_not_repeated_when_already_in_name(plan: dict) -> None:
    """方案名已含风格（「韩式甜美·生日花束」）→ 不再单列一行，避免信息重复。"""
    assert '风格：' not in plan['copy_text']


# ── 2. 金额一致 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('dims', DIMS_CASES)
def test_total_matches_price(dims: dict) -> None:
    """清单合计与 ``plan['price']`` 同源 —— 否则用户对账会对不上。"""
    p = _make(dims)
    assert f"合计约 {p['price']} 元" in p['copy_text']


def test_base_fee_listed_as_one_line(plan: dict) -> None:
    """「包装与手工」（基础费）单独一行、只报金额。

    旧结构里人工费与装饰费是两项，且 detail 带一长串收费依据
    （「含丝带/贺卡/点缀（14 元/束，按预算档标准）」），给店家的清单又长又无关。
    2026-09-18 定价改造（两段式：基础费 + 边际单价）后合并为「包装与手工」一项。
    """
    text = plan['copy_text']
    assert '包装与手工 ——' in text
    assert '按预算档标准' not in text
    assert '· 人工费' not in text


def test_omits_zero_amount_items(plan: dict) -> None:
    """单一花材方案的「配材：无 —— 0 元」不该进清单（无意义的噪音行）。"""
    single = _make({'recipient': '女朋友', 'occasion': '告白', 'budget': '200',
                    'single_flower': '红玫瑰'})
    text = single['copy_text']
    assert '：无 —— 0 元' not in text
    # 逐行判断，不能用全文子串（「150 元」里也含「0 元」，会误报）
    assert not [ln for ln in text.split('\n') if ln.strip().endswith('—— 0 元')]


# ── 3. 不误导 / 合规 ─────────────────────────────────────────────────────

def test_declares_estimate_not_quote(plan: dict) -> None:
    """价格是**估算**，必须写明以门店报价为准 —— 否则等于对外承诺了价格。"""
    text = plan['copy_text']
    assert '估算' in text and '以门店报价为准' in text


def test_declares_ai_generated(plan: dict) -> None:
    """AIGC 标识（GB 45438-2025）：清单是 AI 生成的，须留痕。"""
    assert 'AI 花艺小助手' in plan['copy_text']


def test_marks_unavailable_materials() -> None:
    """锁定店铺时，「该店暂无」的原料要在清单里如实提醒（改版重算同理）。"""
    p = _build_plan(DIMS_CASES[0])
    p['unavailable_materials'] = ['勿忘我', '洋桔梗']
    text = build_plan_copy_text(p)
    assert '勿忘我' in text and '洋桔梗' in text
    assert '暂无' in text


def test_no_unavailable_hint_when_all_available(plan: dict) -> None:
    """没有缺料就不要凭空加提示（会把用户引向不存在的货源问题）。"""
    assert '暂无' not in plan['copy_text']


# ── 4. 不泄漏内部字段 ────────────────────────────────────────────────────

def test_no_internal_keys_leaked(plan: dict) -> None:
    """面向店家的文本里不能出现内部字段名 / 表名（隐私铁律的同类要求）。"""
    text = plan['copy_text']
    for key in ('plan_id', 'budget_breakdown', 'effect_prompt', 'total_estimate',
                'main_flowers', 'unit_price', 'design', 'shop_id', 'DIY_'):
        assert key not in text, f'清单泄漏了内部字段名：{key}'


# ── 5. 边界 / 健壮 ──────────────────────────────────────────────────────

@pytest.mark.parametrize('bad', [{}, None, [], 'text', 0])
def test_invalid_plan_returns_empty(bad) -> None:
    """无效输入返回空串 —— 调用方据此跳过「复制」入口，而不是渲染一个空清单。"""
    assert build_plan_copy_text(bad) == ''


def test_idempotent(plan: dict) -> None:
    """纯函数：同输入同输出（接入方可能重复请求）。"""
    assert build_plan_copy_text(plan) == build_plan_copy_text(plan)


def test_reasonable_length(plan: dict) -> None:
    """清单要能一次性粘进微信窗口 —— 不能是长篇大论。"""
    assert 80 < len(plan['copy_text']) < 800


def test_long_text_truncated_with_marker(plan: dict) -> None:
    """超长必须截断**且留痕**，否则用户以为清单到此为止、拿着漏项去配货。"""
    p = _build_plan(DIMS_CASES[0])
    p['budget_breakdown']['items'] = [
        {'item': f'项目{i}', 'detail': '说' * 60, 'amount': 1} for i in range(80)
    ]
    text = build_plan_copy_text(p)
    assert len(text) <= _COPY_TEXT_MAX + 20
    assert '已截断' in text


# ── 6. 链路：两条产出方案的通路都要带上 ──────────────────────────────────

def test_design_diy_plan_attaches_copy_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import tools as T

    monkeypatch.setattr(
        T, 'design_with_llm',
        lambda *a, **k: T._build_plan(DIMS_CASES[0]))
    plan_out = T.design_diy_plan('送女朋友生日花，预算300', shop_id='')
    assert plan_out.get('copy_text'), 'generate_diy_plan 链路没有产出复制清单'
    assert '【定制花束需求单】' in plan_out['copy_text']


def test_copy_text_generated_after_shop_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """清单必须在**缺料标注之后**生成。

    顺序写反的话，清单会漏掉「该店暂无」提醒 —— 用户拿着一份该店配不齐的
    清单去沟通，这正是我们要避免的。
    """
    from agent import shop_materials
    from agent import tools as T

    monkeypatch.setattr(
        T, 'design_with_llm',
        lambda *a, **k: T._build_plan(DIMS_CASES[0]))
    # 该店明确只有绣球（可判定），且方案花材都识别不出来 → 全部判为缺料
    monkeypatch.setattr(shop_materials, 'available_materials', lambda sid: {'绣球'})
    monkeypatch.setattr(shop_materials, 'materials_in_text', lambda t: set())

    plan_out = T.design_diy_plan('送女朋友生日花，预算300', shop_id='s1')
    assert plan_out.get('unavailable_materials'), '前置条件未成立：应标注缺料'
    assert '暂无' in plan_out['copy_text'], '清单生成早于缺料标注 → 漏掉提醒'


def test_revise_diy_plan_recomputes_copy_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """改版换过花材/预算后，清单必须跟着重算 —— 旧清单就是错的。"""
    from agent import diy_tools
    from agent import tools as T

    monkeypatch.setattr(
        T, 'revise_with_llm',
        lambda plan, feedback, shop_id='': T._build_plan(
            {'recipient': '妈妈', 'occasion': '母亲节', 'budget': '200'}))
    raw = asyncio.run(diy_tools.revise_diy_plan('{}', '便宜点', {}))
    data = json.loads(raw)
    assert data.get('copy_text'), 'revise_diy_plan 链路没有产出复制清单'
    assert f"合计约 {data['price']} 元" in data['copy_text']


def test_copy_text_does_not_alter_existing_plan_fields(plan: dict) -> None:
    """只新增字段，不改动既有字段 —— 避免影响已对接的渲染。"""
    before = {k: v for k, v in plan.items() if k != 'copy_text'}
    again = build_plan_copy_text(plan)
    assert again
    after = {k: v for k, v in plan.items() if k != 'copy_text'}
    assert before == after
