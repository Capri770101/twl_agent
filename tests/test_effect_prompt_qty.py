"""效果图提示词带支数回归门（2026-09-16 用户反馈）。

用户要求「效果图的花的数量也要和方案保持一致」。原实现只把**花名**写进
``effect_prompt``，数量交给生图模型自由发挥 → 效果图里的花量与方案清单对不上。

这里保护 ``_effect_prompt_from_design`` 与 ``_merge_plan`` 末尾的重建
（纯函数，不连 LLM / DB / 网络）。
"""
from __future__ import annotations

from agent.tools import (
    _build_plan,
    _design_qty_map,
    _effect_prompt_from_design,
    _merge_plan,
)


def test_prompt_carries_every_qty_and_total():
    d = {
        'main_flowers': [{'name': '粉色康乃馨', 'qty': 22}, {'name': '粉玫瑰', 'qty': 5}],
        'fillers': [{'name': '满天星', 'qty': 3}],
        'foliage': [{'name': '尤加利', 'qty': 2}],
        'color_scheme': ['粉', '香槟'],
    }
    p = _effect_prompt_from_design(d, '韩式', '雾面韩素纸')
    for frag in ('粉色康乃馨 22 枝', '粉玫瑰 5 枝', '满天星 3 枝', '尤加利 2 枝'):
        assert frag in p, f'{frag} 没进提示词：{p}'
    assert '整束共 32 枝' in p
    assert '粉/香槟' in p and '雾面韩素纸' in p


def test_single_flower_prompt_has_qty_without_extra_fluff():
    p = _effect_prompt_from_design({'main_flowers': [{'name': '康乃馨', 'qty': 33}]}, '韩式', '礼盒')
    assert '康乃馨' in p and '33 枝' in p
    # 单一花材不得残留「搭配满天星」这类外搭措辞
    assert '满天星' not in p and '搭配' not in p


def test_prompt_degrades_for_legacy_rows_without_qty():
    """老数据没有 qty：不能报错，也不能凭空编出数量。"""
    p = _effect_prompt_from_design({'main_flowers': [{'name': '玫瑰'}]}, '韩式', '花束')
    assert '玫瑰' in p
    assert '枝' not in p


def test_prompt_never_blank():
    for d in ({}, {'main_flowers': []}, {'main_flowers': ['玫瑰']}):
        assert _effect_prompt_from_design(d).strip(), d


def test_merge_plan_rebuilds_prompt_from_final_qty():
    """_merge_plan 之后，提示词的支数必须等于**校正后**的最终清单支数。"""
    baseline = _build_plan({'recipient': '母亲', 'occasion': '生日', 'budget': '200', 'colors': ['粉']})
    # LLM 自报康乃馨 ×12：终值由预算分配决定，提示词不能被它带偏
    llm_plan = {'design': {'main_flowers': [{'name': '康乃馨', 'qty': 12}]}}
    plan = _merge_plan(baseline, llm_plan)

    qmap = _design_qty_map(plan['design'])
    prompt = plan['effect_prompt']
    assert qmap, '合并后应能拿到支数表'
    for name, n in qmap.items():
        assert f'{name} {n} 枝' in prompt, f'{name} 的支数没进提示词：{prompt}'
    assert '康乃馨 12 枝' not in prompt or qmap.get('康乃馨') == 12
