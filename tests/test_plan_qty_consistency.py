"""方案支数一致性回归门（2026-09-16 线上缺陷）。

线上实测：演示实例的定制方案里，描述写「粉色康乃馨×12 配洋桔梗×4 及尤加利叶×3」，
而花材清单写「康乃馨×3 枝、洋桔梗×2 枝」——LLM 的 desc/diy_steps 按**它自己**的支数写，
最终支数却由预算分配决定。客户一眼就能看出自相矛盾。

这里保护 `_sync_flower_qty` 与 `_design_qty_map`（纯函数，不连 LLM/DB/网络）。
"""
from __future__ import annotations

from agent.tools import (
    _build_plan,
    _design_qty_map,
    _merge_plan,
    _sync_flower_qty,
)


def _design(*flowers: tuple[str, int]) -> dict:
    return {'main_flowers': [{'name': n, 'qty': q} for n, q in flowers]}


# ── _design_qty_map ───────────────────────────────────────────────────

def test_qty_map_collects_all_groups():
    d = {
        'main_flowers': [{'name': '康乃馨', 'qty': 3}],
        'fillers': [{'name': '洋桔梗', 'qty': 2}],
        'foliage': [{'name': '尤加利叶', 'qty': 1}],
    }
    assert _design_qty_map(d) == {'康乃馨': 3, '洋桔梗': 2, '尤加利叶': 1}


def test_qty_map_ignores_malformed_entries():
    d = {
        'main_flowers': [{'name': '', 'qty': 3}, {'qty': 2}, 'not-a-dict', {'name': '玫瑰', 'qty': 0}],
    }
    assert _design_qty_map(d) == {}


# ── _sync_flower_qty ──────────────────────────────────────────────────

def test_sync_rewrites_name_times_n():
    qty = {'康乃馨': 3, '洋桔梗': 2, '尤加利叶': 1}
    src = '粉色康乃馨×12 配洋桔梗×4 及尤加利叶×3，柔和暖调。'
    assert _sync_flower_qty(src, qty) == '粉色康乃馨×3 配洋桔梗×2 及尤加利叶×1，柔和暖调。'


def test_sync_rewrites_count_before_name():
    """「11朵粉玫瑰」这类写法也要校正，且保留颜色修饰词。"""
    qty = {'玫瑰': 9}
    assert _sync_flower_qty('11朵粉玫瑰打底', qty) == '粉玫瑰×9打底'
    assert _sync_flower_qty('9支玫瑰', qty) == '玫瑰×9'


def test_sync_leaves_other_numbers_alone():
    """预算、天数、厘米这些数字不能被误改。"""
    qty = {'康乃馨': 3}
    src = '康乃馨共 3 支，预算 200 元，花期约 7-10 天，斜剪根部 2cm。'
    out = _sync_flower_qty(src, qty)
    assert '预算 200 元' in out and '7-10 天' in out and '2cm' in out


def test_sync_prefers_longer_flower_name():
    """「粉玫瑰」与「玫瑰」同时存在时，不能把「粉玫瑰」误改成「玫瑰×N」。"""
    qty = {'玫瑰': 5, '粉玫瑰': 9}
    assert _sync_flower_qty('粉玫瑰×2 与玫瑰×1', qty) == '粉玫瑰×9 与玫瑰×5'


def test_sync_noop_on_empty():
    assert _sync_flower_qty('', {'玫瑰': 3}) == ''
    assert _sync_flower_qty('玫瑰×3', {}) == '玫瑰×3'


# ── _merge_plan 端到端一致性 ──────────────────────────────────────────

def test_merge_plan_makes_desc_consistent_with_allocated_qty():
    """LLM 的描述支数必须被方案真实支数覆盖（这是线上那个 bug 的直接回归门）。"""
    baseline = _build_plan({'recipient': '母亲', 'occasion': '生日', 'budget': '200', 'colors': ['粉']})
    real = baseline['design']['main_flowers']
    assert real, 'baseline 应产出主花'

    llm_plan = {
        'name': '暖阳康乃馨·温馨感恩花束',
        'desc': '粉色康乃馨×12 配洋桔梗×4 及尤加利叶×3，柔和暖调。',
        'design': {
            'main_flowers': [{'name': '康乃馨', 'qty': 12, 'flower_language': ['母爱']}],
            'diy_steps': ['康乃馨×12：去除多余叶片，斜剪根部 2cm。'],
        },
    }
    plan = _merge_plan(baseline, llm_plan)

    qty = _design_qty_map(plan['design'])
    assert qty, '合并后应能拿到支数表'
    for name, n in qty.items():
        assert f'{name}×{n}' in plan['desc'] or f'{name}×' not in plan['desc'], \
            f'desc 里的 {name} 支数与方案不一致：{plan["desc"]}'
    # 明确的断言：描述里不应再出现 LLM 自报的 ×12
    assert '康乃馨×12' not in plan['desc']
    # diy_steps 同样被校正
    steps = plan.get('diy_steps') or []
    assert all('康乃馨×12' not in str(s) for s in steps)


def test_merge_plan_keeps_other_text_untouched():
    baseline = _build_plan({'recipient': '恋人', 'occasion': '告白', 'budget': '300'})
    plan = _merge_plan(baseline, {'desc': '这束花寓意浪漫与承诺，预算 300 元。'})
    assert '预算 300 元' in plan['desc']
    assert '浪漫与承诺' in plan['desc']
