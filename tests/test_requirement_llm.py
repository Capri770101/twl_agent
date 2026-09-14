"""L2 回归门：需求抽取 = 「LLM 结构化补召回 + 正则兜底」。

两条不变量：
1. **只补空、不覆盖**——规则（正则）一旦抽到值，模型的话不能改写它（确定性优先）；
2. **值域白名单**——模型补进来的每个值都要过校验（复用规则表值集 / 已知花名表），
   幻觉字段一律丢弃，绝不让"猜的值"污染下游（价格档、支数、单一花材约束都吃这个）。

外加中文数字支数的兜底（真实缺口：正则原本只认阿拉伯数字，『十一朵』抽不到）。
"""

from __future__ import annotations

from agent.tools import (
    _LLM_REQUIREMENT_HINT,
    _cn_to_int,
    _extract_stem_count,
    _get_scene_map,
    extract_requirement,
    merge_requirement,
)


# ── 1. 中文数字支数（正则兜底加强）──

def test_cn_to_int() -> None:
    assert _cn_to_int('两') == 2
    assert _cn_to_int('九') == 9
    assert _cn_to_int('十') == 10
    assert _cn_to_int('十一') == 11
    assert _cn_to_int('二十') == 20
    assert _cn_to_int('二十五') == 25
    assert _cn_to_int('') is None
    assert _cn_to_int('百') is None


def test_extract_stem_count_covers_chinese_numerals() -> None:
    # 此前缺口：『十一朵粉玫瑰』抽不到支数
    assert _extract_stem_count('十一朵粉玫瑰') == 11
    assert _extract_stem_count('要二十支') == 20
    assert _extract_stem_count('十枝就好') == 10
    assert _extract_stem_count('两朵') == 2
    # 既有语义必须保持不变：一束 = 11 支、一打 = 12 支（不能被中文数字解析成 1）
    assert _extract_stem_count('一束花') == 11
    assert _extract_stem_count('一打玫瑰') == 12
    assert _extract_stem_count('11朵') == 11
    assert _extract_stem_count('送妈妈一束花') == 11
    assert _extract_stem_count('想买花') is None
    # 边界钳制
    assert _extract_stem_count('9999朵') == 999


# ── 2. merge_requirement：只补空、值域白名单 ──

def test_merge_returns_baseline_when_llm_payload_invalid() -> None:
    req = extract_requirement('送给妈妈的花')
    assert merge_requirement(req, None) is req
    assert merge_requirement(req, 'not-a-dict') is req
    # 合法 dict → 返回新对象且不修改入参
    empty = merge_requirement(req, {})
    assert empty is not req and empty.recipient == req.recipient and req.relationship == '亲子'


def test_merge_never_overrides_rule_values() -> None:
    req = extract_requirement('送给妈妈的花，预算200')
    assert req.recipient == '母亲' and req.budget_num == 200
    merged = merge_requirement(req, {'recipient': '恋人', 'budget': 5000, 'occasion': '告白'})
    # 规则已抽到的值不得被模型改写
    assert merged.recipient == '母亲'
    assert merged.budget_num == 200
    # 规则没抽到的场合 → 可以由模型补
    assert merged.occasion == '告白'


def test_merge_fills_only_whitelisted_values() -> None:
    req = extract_requirement('想买花')
    merged = merge_requirement(req, {
        'recipient': '丈母娘',        # 非法值 → 丢弃
        'occasion': '探病',           # 合法
        'style': 'S_JAPANESE',        # 合法
        'colors': ['粉', '彩虹', '蓝'],  # 过滤非法色
        'mood': '莫名其妙',            # 非法 → 丢弃
    })
    assert merged.recipient is None
    assert merged.occasion == '探病'
    assert merged.style == 'S_JAPANESE'
    assert merged.colors == ['粉', '蓝']
    assert merged.mood is None


def test_merge_budget_and_stem_validation() -> None:
    req = extract_requirement('想买束花')
    m1 = merge_requirement(req, {'budget': '约200元'})     # 宽容解析字符串金额
    assert m1.budget_num == 200 and m1.budget_min == 160 and m1.budget_max == 240
    assert merge_requirement(req, {'budget': 5}).budget_num is None        # 太低 → 丢弃
    assert merge_requirement(req, {'budget': 99999}).budget_num is None    # 太高 → 丢弃
    assert merge_requirement(req, {'budget': '没提'}).budget_num is None
    assert merge_requirement(req, {'stem_count': '十一'}).stem_count == 11  # 中文数字
    assert merge_requirement(req, {'stem_count': -3}).stem_count is None
    assert merge_requirement(req, {'stem_count': 9999}).stem_count is None


def test_merge_single_flower_must_be_known_flower() -> None:
    req = extract_requirement('想要一束花')
    assert merge_requirement(req, {'single_flower': '红玫瑰'}).single_flower == '红玫瑰'
    assert merge_requirement(req, {'single_flower': '彩虹花'}).single_flower is None
    # 规则已识别出单花时，模型不能改。
    # 注：「纯白百合」夹了颜色字，花名按通用名表返回「百合」，颜色由 colors 承载（'白'）。
    single = extract_requirement('就要纯白百合')
    assert single.single_flower == '百合' and single.colors == ['白']
    assert merge_requirement(single, {'single_flower': '红玫瑰'}).single_flower == '百合'


def test_merge_scene_and_relationship() -> None:
    req = extract_requirement('想买束花')
    any_scene = next(iter(_get_scene_map().values()))
    assert merge_requirement(req, {'scene': any_scene}).scene == any_scene
    assert merge_requirement(req, {'scene': '外星球'}).scene is None
    # 收花人补上后，关系标签需同步推导（RAG 检索依赖它）
    merged = merge_requirement(req, {'recipient': '长辈'})
    assert merged.relationship == '长辈/同事'


# ── 3. prompt 提示与开关（防止将来被误删/改语义）──

def test_requirement_hint_demands_no_guessing() -> None:
    assert 'requirements' in _LLM_REQUIREMENT_HINT
    assert 'null' in _LLM_REQUIREMENT_HINT
    assert '绝不根据常识推测' in _LLM_REQUIREMENT_HINT


def test_config_switch_exists() -> None:
    from backend.config import settings
    assert hasattr(settings, 'DIY_LLM_REQUIREMENT_ENABLED')
