"""「只有文字、没有方案卡」拦截器的单测（2026-09-16 线上缺陷回归门）。

背景：演示实例上，用户先拿到方案卡，再补一句「生日，粉色系，你决定就好」，
模型**零工具调用**、3.7 秒直接写了两款"方案"（其中一款商品名平台上根本不存在）。
这里保护的是确定性拦截逻辑——纯函数，不连 DB / LLM / 网络。
"""
from __future__ import annotations

from agent.agent import (
    _card_nudge_text,
    _card_produced,
    _expresses_flower_need,
    _needs_card_nudge,
)


class _TC:
    """最小 ToolCallRecord 替身（函数只读 name / arguments / status）。"""

    def __init__(self, name: str, status: str = 'ok', **arguments) -> None:
        self.name = name
        self.status = status
        self.arguments = arguments


# ── _expresses_flower_need ─────────────────────────────────────────────

def test_flower_need_detects_requirement_slots():
    assert _expresses_flower_need('生日，粉色系，你决定就好')     # 场合 + 颜色
    assert _expresses_flower_need('送妈妈一束花，预算200')        # 收花人 + 预算
    assert _expresses_flower_need('预算改成300元')                # 预算
    assert _expresses_flower_need('换成白色的')                   # 颜色
    assert _expresses_flower_need('换成韩式风格')                 # 风格
    assert _expresses_flower_need('11朵粉玫瑰')                   # 支数 + 单一花材


def test_flower_need_ignores_non_requirement():
    assert not _expresses_flower_need('谢谢')
    assert not _expresses_flower_need('')
    assert not _expresses_flower_need('这束花怎么养护？')
    assert not _expresses_flower_need('你叫什么名字')


# ── _card_produced ────────────────────────────────────────────────────

def test_card_produced_by_plan_tools():
    assert _card_produced([_TC('generate_diy_plan')])
    assert _card_produced([_TC('revise_diy_plan')])
    assert _card_produced([_TC('show_plan_card')])


def test_card_produced_by_platform_query():
    assert _card_produced([_TC('platform_db_query_entity', entity='plan')])
    assert _card_produced([_TC('platform_db_query_entity', entity='shop')])
    # 订单查询不算「方案卡」证据
    assert not _card_produced([_TC('platform_db_query_entity', entity='order')])


def test_card_produced_ignores_failures_and_other_tools():
    assert not _card_produced([_TC('generate_diy_plan', status='error')])
    assert not _card_produced([_TC('retrieve_knowledge'), _TC('respond_to_user')])
    assert not _card_produced([])


# ── _needs_card_nudge ─────────────────────────────────────────────────

def test_nudge_fires_when_context_exists_and_need_expressed():
    """线上复现的那一轮：有方案上下文 + 用户表达需求 + 没出卡 → 必须拦。"""
    assert _needs_card_nudge('生日，粉色系，你决定就好', [], has_context=True)


def test_nudge_silent_without_context():
    """没有方案上下文时不拦——首次提问用文字追问是合理行为。"""
    assert not _needs_card_nudge('生日，粉色系，你决定就好', [], has_context=False)


def test_nudge_silent_when_card_produced():
    assert not _needs_card_nudge('换成粉色', [_TC('revise_diy_plan')], has_context=True)
    assert not _needs_card_nudge('推荐送妈妈的', [_TC('platform_db_query_entity', entity='plan')],
                                 has_context=True)


def test_nudge_silent_for_knowledge_questions():
    """知识类问句本来就该用文字答，不能被拉回去出卡。"""
    for q in ('这束花的花语是什么', '为什么选康乃馨', '怎么养护能开更久', '这个方案和那个有什么区别'):
        assert not _needs_card_nudge(q, [], has_context=True), q


def test_nudge_silent_for_chitchat():
    assert not _needs_card_nudge('谢谢，很满意', [], has_context=True)
    assert not _needs_card_nudge('好的', [], has_context=True)


# ── 纠正文案 ──────────────────────────────────────────────────────────

def test_nudge_text_mentions_tools_and_forbids_fabrication():
    txt = _card_nudge_text()
    assert 'generate_diy_plan' in txt and 'revise_diy_plan' in txt
    assert 'platform_db_query_entity' in txt
    assert '严禁' in txt and '编造' in txt
