"""卡片类回复的「要点兜底」测试。

背景
----
实测模型在带卡片时强烈倾向只回「已推给你，点卡片看」而不给出结论；已尝试 3 处 prompt
强化（回复格式 / 全平台模式 / 场景6）但生成有随机性、效果不稳定。因此在代码层补确定性
兜底：reply 明显过短且带卡片时，从卡片数据取可读字段拼成简短要点。

本测试锁住三个边界：只对卡片类型生效、reply 够长时不打扰、绝不带出内部标识。
"""
from __future__ import annotations

from agent.agent import _CARD_SUMMARY_MIN_REPLY, _ensure_card_summary
from agent.engine.ui_protocol import UIType


def test_short_plan_card_reply_gets_summary() -> None:
    data = {'plans': [{'name': 'Kitty玫瑰心动桶', 'price': 168}, {'name': '玫瑰恋人', 'price': 198}]}
    out = _ensure_card_summary('已把卡片推给你了。', UIType.PLAN_CARD, data)
    assert 'Kitty玫瑰心动桶' in out
    assert '168' in out
    assert '已把卡片推给你了。' in out          # 模型原话必须保留


def test_short_shop_card_reply_gets_summary() -> None:
    data = {'shops': [{'name': '千百度花坊', 'rating': 4.8, 'price_range': '100-300'}]}
    out = _ensure_card_summary('看卡片～', UIType.SHOP_CARD, data)
    assert '千百度花坊' in out
    assert '4.8' in out


def test_long_reply_untouched() -> None:
    reply = '推荐这几家：千百度花坊（4.8分）、花美家（4.7分），都在你附近，可以点卡片看详情。'
    assert len(reply) > _CARD_SUMMARY_MIN_REPLY
    assert _ensure_card_summary(reply, UIType.SHOP_CARD, {'shops': [{'name': 'X'}]}) == reply


def test_text_ui_untouched() -> None:
    assert _ensure_card_summary('短', UIType.TEXT, {'plans': [{'name': 'A', 'price': 1}]}) == '短'


def test_empty_card_data_untouched() -> None:
    assert _ensure_card_summary('看卡片', UIType.PLAN_CARD, {'plans': []}) == '看卡片'


def test_empty_reply_builds_summary() -> None:
    out = _ensure_card_summary('', UIType.PLAN_CARD, {'plans': [{'name': 'A', 'price': 100}]})
    assert 'A' in out and '100' in out


def test_no_internal_identifiers_in_summary() -> None:
    """补充内容只能来自可读字段，绝不能带出 shop_id / plan_id。"""
    data = {'shops': [{'shop_id': 'S001', 'name': '花店', 'rating': 5.0}]}
    out = _ensure_card_summary('看卡片', UIType.SHOP_CARD, data)
    assert 'S001' not in out
    assert '花店' in out


def test_missing_fields_skipped_gracefully() -> None:
    data = {'plans': [{}, {'name': ''}, {'name': '有效方案', 'price': None}]}
    out = _ensure_card_summary('看卡片', UIType.PLAN_CARD, data)
    assert '有效方案' in out
