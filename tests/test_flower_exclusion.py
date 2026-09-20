"""「不要满天星还是给我满天星」回归门（2026-09-20 生产实测缺陷）。

## 用户原话复现

```
我：我想diy
助手：（给出「初遇微光」，含 满天星×2）
我：不要满天星
助手：（回复里自己承认「满天星在配材清单里还残留了一条」）
     卡片「配材 · 叶材　满天星×5；尤加利×5」   ← 说了不要还是给了
```

## 根因（两处叠加，缺一不可）

1. **baseline 的兜底会把排除项塞回来**：`fillers = [...][:1] or [满天星]` ——
   候选里没有别的填充花材时，无条件回落成满天星，哪怕用户明确不要它。
2. **合并阶段没有重放排除**：`_build_plan(exclude_flowers=…)` 只保证 **baseline** 干净，
   而 `_merge_plan` 是「LLM 有则覆盖」—— 模型照样能把被排除的花材写回来。
   改版 prompt 也**从未**把「不要满天星」作为硬性约束告诉模型（只有单一花材/支数/锁店有）。

## 修复后的三层保证

| 层 | 机制 | 兜住什么 |
|---|---|---|
| 提示词 | `_exclusion_rule()` 写进硬性约束 | 让模型一开始就别写 |
| 基线 | `_fallback_flower()` 尊重排除（宁可留空） | 规则引擎不自己加回来 |
| 合并后 | `_apply_flower_exclusions()` 确定性剔除 + `_scrub_excluded_mentions()` 清文字 | 模型不听话时兜住 |

另：排除项会**持久化到 `plan['exclude_flowers']`** 并在改版时并集继承 ——
否则用户下一轮只说「换个配色」，满天星就又回来了。
"""
from __future__ import annotations

import json

from agent.tools import (
    _apply_flower_exclusions,
    _build_plan,
    _desc_from_design,
    _extract_feedback,
    _fallback_flower,
    _meaning_from_design,
    _mentions_excluded,
    _merge_plan,
    _resolve_flowers,
    _scrub_excluded_mentions,
)

DIMS = {'recipient': '恋人', 'occasion': '告白', 'budget': '150', 'colors': ['粉']}
EX_MX = {'满天星'}


def _plan(exclude=None, **kw):
    return _build_plan(dict(DIMS), exclude_flowers=exclude, **kw)


def _names(plan, group):
    return [f['name'] for f in (plan.get('design') or {}).get(group) or []]


def _all_flower_names(plan):
    d = plan.get('design') or {}
    return _names(plan, 'main_flowers') + _names(plan, 'fillers') + _names(plan, 'foliage')


# ── 第一层：解析「不要 X」───────────────────────────────────────────────

def test_extract_feedback_parses_exclusion():
    assert '满天星' in _extract_feedback('不要满天星')['exclude']
    assert '满天星' in _extract_feedback('去掉满天星吧')['exclude']
    assert _extract_feedback('换个配色')['exclude'] == set()


# ── 第二层：基线兜底不得把排除项加回来 ──────────────────────────────────

def test_fallback_flower_respects_exclusion():
    """⚠️ 核心回归：首选被排除时改取同类其他花材；一个都没有就**留空**。"""
    all_flowers = {'满天星': {'name': '满天星', 'category': '填充'},
                   '勿忘我': {'name': '勿忘我', 'category': '填充'}}
    out = _fallback_flower(all_flowers, '填充', '满天星', {'满天星'})
    assert [f['name'] for f in out] == ['勿忘我']

    only_excluded = {'满天星': {'name': '满天星', 'category': '填充'}}
    assert _fallback_flower(only_excluded, '填充', '满天星', {'满天星'}) == []


def test_baseline_plan_has_no_excluded_flower():
    plan = _plan(EX_MX)
    assert '满天星' not in _all_flower_names(plan), f'基线仍含满天星：{_all_flower_names(plan)}'


def test_baseline_persists_exclusion_for_next_turn():
    """持久化：改版时要继承（否则下一轮只说「换个配色」满天星就回来了）。"""
    assert _plan(EX_MX).get('exclude_flowers') == ['满天星']


def test_resolve_flowers_exclusion_end_to_end():
    from agent.tools import _get_style_full, _get_tier
    style, _ = _get_style_full('S_KOREAN')
    tier = _get_tier(150, None)
    main, fillers, foliage = _resolve_flowers(
        {'recipient': '恋人', 'occasion': '告白', 'colors': ['粉'], 'color': '粉'},
        style, tier, exclude_flowers=EX_MX)
    assert '满天星' not in [f['name'] for f in fillers]


# ── 第三层：合并后确定性剔除（模型写回来也要剔）──────────────────────────

def _llm_plan_with(name: str, qty: int = 5, extra: dict | None = None) -> dict:
    """模拟「模型无视排除、照样把满天星写进 fillers」的输出。"""
    plan = {
        'name': '初遇微光', 'style': '韩式', 'desc': f'粉玫瑰配{name}，温柔告白',
        'design': {
            'main_flowers': [{'name': '粉玫瑰', 'qty': 6}],
            'fillers': [{'name': name, 'qty': qty}],
            'foliage': [{'name': '尤加利', 'qty': 2}],
            'color_scheme': ['粉', '白'],
            'packaging': '雾面纸',
            'meaning': f'粉玫瑰代表心动，{name}点缀',
        },
    }
    plan.update(extra or {})
    return plan


def test_merge_plan_strips_excluded_flower_written_by_llm():
    """⚠️ 核心回归：baseline 干净 ≠ 合并结果干净（LLM 会覆盖）。"""
    baseline = _plan(EX_MX)
    merged = _merge_plan(baseline, _llm_plan_with('满天星'), exclude_flowers=EX_MX)
    assert '满天星' not in _all_flower_names(merged), f'模型写回的满天星没被剔除：{_all_flower_names(merged)}'


def test_apply_flower_exclusions_strips_all_groups():
    plan = {'design': {
        'main_flowers': [{'name': '满天星', 'qty': 3}, {'name': '粉玫瑰', 'qty': 6}],
        'fillers': [{'name': '满天星', 'qty': 5}],
        'foliage': [{'name': '尤加利', 'qty': 2}],
        'notes': ['已按反馈移除：满天星', '本方案共 16 支'],
    }}
    removed = _apply_flower_exclusions(plan, EX_MX)
    assert removed.count('满天星') == 2
    assert plan['design']['main_flowers'] == [{'name': '粉玫瑰', 'qty': 6}]
    assert plan['design']['fillers'] == []
    assert not any('满天星' in n for n in plan['design']['notes'])


def test_apply_flower_exclusions_keeps_main_when_emptied():
    """用户说「不要玫瑰」且没有别的候选 → 主花不能空（回落 baseline 的干净主花）。"""
    plan = {'design': {'main_flowers': [{'name': '玫瑰', 'qty': 9}], 'fillers': []}}
    baseline_design = {'main_flowers': [{'name': '康乃馨', 'qty': 9}]}
    _apply_flower_exclusions(plan, {'玫瑰'}, baseline_design)
    assert [f['name'] for f in plan['design']['main_flowers']] == ['康乃馨']


# ── 文字字段：正面提及要清，否定表述要留 ────────────────────────────────

def test_mentions_excluded_ignores_negation():
    """⚠️ 关键反例：模型写「去除了满天星的繁复感」是**正确**表述，不能算提及。"""
    assert _mentions_excluded('去除了满天星的繁复感，只留纯粹温柔', ['满天星']) is False
    assert _mentions_excluded('不含满天星', ['满天星']) is False
    assert _mentions_excluded('粉玫瑰点缀满天星与尤加利', ['满天星']) is True


def test_scrub_rewrites_desc_positively_mentioning_excluded():
    plan = {'style': '韩式', 'design': {
        'main_flowers': [{'name': '粉玫瑰', 'qty': 6}],
        'fillers': [], 'foliage': [{'name': '尤加利', 'qty': 2}],
        'color_scheme': ['粉'], 'packaging': '雾面纸',
        'meaning': '心动、热烈',
    }, 'desc': '粉玫瑰配满天星，温柔告白', 'diy_steps': ['玫瑰斜剪', '插入满天星点缀']}
    _scrub_excluded_mentions(plan, EX_MX)
    assert '满天星' not in plan['desc']
    assert all('满天星' not in s for s in plan['diy_steps'])
    assert '尤加利' in plan['desc'] or '粉玫瑰' in plan['desc']


def test_scrub_keeps_negated_desc():
    """否定句必须原样保留（否则会把对的改错）。"""
    original = '去除了满天星的繁复感，保留粉玫瑰与白洋桔梗的清雅组合'
    plan = {'style': '韩式', 'design': {}, 'desc': original, 'diy_steps': []}
    _scrub_excluded_mentions(plan, EX_MX)
    assert plan['desc'] == original


def test_desc_and_meaning_rebuild_are_consistent():
    """剔除之后再重建 → 描述/寓意里不会留下被排除的花材。"""
    plan = {'style': '韩式', 'design': {
        'main_flowers': [{'name': '粉玫瑰', 'qty': 6, 'flower_language': ['心动']}],
        'fillers': [{'name': '满天星', 'qty': 2}],
        'color_scheme': ['粉', '白'], 'packaging': '雾面纸',
    }}
    _apply_flower_exclusions(plan, EX_MX)
    assert '满天星' not in _desc_from_design(plan)
    assert '粉玫瑰×6' in _desc_from_design(plan)
    assert _meaning_from_design(plan['design']) == '心动'


# ── 剔除后价格必须跟着收口（不能「用料去掉了、报价还是旧的」）──────────

def test_price_stays_consistent_after_exclusion():
    baseline = _plan(EX_MX)
    merged = _merge_plan(baseline, _llm_plan_with('满天星', 5), exclude_flowers=EX_MX)
    bb = merged.get('budget_breakdown') or {}
    total = sum((it.get('amount') or 0) for it in (bb.get('items') or []))
    assert total == bb.get('total_estimate') == merged.get('price')
    # 明细里也不能再出现满天星
    assert '满天星' not in json.dumps(bb, ensure_ascii=False)
