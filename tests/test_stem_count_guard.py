"""护栏回归：模型**凭空补出**的支数必须被丢弃。

背景（2026-09-18 线上实测发现）：prompt 里给模型的示例「『11 朵』→ 11」被当成了
默认值填回来 —— 两条完全不同、且**都没提支数**的请求（预算 300 / 预算 500）
都「补召回」出 ``stem_count=11``。

支数是强约束（`_alloc_stems` 里用户明确支数优先、直接跳过「按预算反推」），
实测代价：
- 一束 299 元的方案被算成 **101 元**（差 198 元）；
- 模型随后察觉不对，又自己调一轮 `revise_diy_plan` 去救
  —— **既报错价、又白烧一整轮 LLM**（单轮 DIY 因此从 ~30s 涨到 60~150s）。
"""

from __future__ import annotations

import pytest

from agent.tools import (
    _build_plan,
    _extract_stem_count,
    _reject_hallucinated_stem_count,
    extract_requirement,
)

NO_STEM_TEXT = '帮我定制一束送妈妈的花，预算300'


def _req(text: str = NO_STEM_TEXT):
    return extract_requirement(text)


def test_rejects_stem_count_absent_from_text() -> None:
    """原话里没有支数 → 模型补的必须丢弃。"""
    merged = _req()
    merged.stem_count = 11
    out = _reject_hallucinated_stem_count(_req(), merged, NO_STEM_TEXT)
    assert out.stem_count is None


@pytest.mark.parametrize('text,value', [
    ('帮我包 11 朵玫瑰，预算300', 11),
    ('就要十一朵红玫瑰', 11),
    ('来 33 支粉玫瑰', 33),
])
def test_keeps_stem_count_actually_said(text: str, value: int) -> None:
    """原话里真有这个数 → 保留（不能把用户明说的约束也误杀）。"""
    merged = _req(text)
    merged.stem_count = value
    out = _reject_hallucinated_stem_count(_req(text), merged, text)
    assert out.stem_count == value


def test_keeps_when_rule_already_extracted() -> None:
    """规则引擎（含会话累积）已抽到支数 → 说明用户确实说过，不做校验。

    （跨轮场景：用户第一轮说「11 朵」，第二轮只说「换个颜色」，
    此时 ``rule_req.stem_count`` 来自会话累积，必须在场。）
    """
    rule = _req('11 朵玫瑰')
    assert rule.stem_count == 11
    merged = _req('11 朵玫瑰')
    merged.stem_count = 11
    out = _reject_hallucinated_stem_count(rule, merged, '换个颜色')   # 原话里没有 11
    assert out.stem_count == 11, '会话累积来的支数不该被误杀'


def test_ignores_when_model_said_null() -> None:
    """模型本来就填 null → 无操作。"""
    merged = _req()
    out = _reject_hallucinated_stem_count(_req(), merged, NO_STEM_TEXT)
    assert out.stem_count is None


def test_price_impact_is_real_and_guarded() -> None:
    """端到端证明：幻觉支数会打歪报价，护栏能挡住。

    这条是这组测试存在的理由 —— 报价差 198 元不是抽象的。
    """
    dims = {'recipient': '妈妈', 'occasion': '生日', 'budget': '300'}
    correct = _build_plan(dict(dims))
    polluted = _build_plan({**dims, 'stem_count': '11'})
    # 没有支数时，花量由预算反推 → 报价贴近预算
    assert correct['price'] > 200
    # 被幻觉支数污染后，花量被钉死在 11 支 → 报价崩到三分之一
    assert polluted['price'] < correct['price'] / 2, '污染未产生预期影响，测试前提已变'


def test_extract_stem_count_matches_guard_semantics() -> None:
    """护栏依赖 `_extract_stem_count`，它的口径要覆盖阿拉伯与中文数字。"""
    assert _extract_stem_count('11 朵玫瑰') == 11
    assert _extract_stem_count('十一朵玫瑰') == 11
    assert _extract_stem_count(NO_STEM_TEXT) is None
    assert _extract_stem_count('预算300') is None
