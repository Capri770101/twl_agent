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
    _looks_like_plan_prose,
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


# ── 首轮豁免的收口（2026-09-16）────────────────────────────────────────
# 首轮 has_context 为假，护栏此前**整体豁免**；但模型常常首轮就零工具调用地口述整套方案。

def test_looks_like_plan_prose_detects_oral_plan():
    """线上那句：「香槟色洋桔梗×5 + 紫色风信子×5…预算 318 元」。"""
    assert _looks_like_plan_prose('给你女朋友设计了一份方案：香槟色洋桔梗×5 + 紫色风信子×5，预算318元')
    assert _looks_like_plan_prose('22朵粉康乃馨配5朵粉玫瑰，约158元')
    assert _looks_like_plan_prose('33枝粉色康乃馨，总价146元')


def test_looks_like_plan_prose_ignores_clarifying_question():
    """老实追问不能被当成口述方案——否则「信息不够先问」会被拦成硬出卡。"""
    assert not _looks_like_plan_prose('方便告诉我送给谁、什么场合吗？预算大概多少呢？')
    assert not _looks_like_plan_prose('你想送妈妈还是女朋友？')
    assert not _looks_like_plan_prose('')
    assert not _looks_like_plan_prose('好的，我来帮你安排～')


def test_nudge_fires_on_first_turn_when_model_dictates_plan():
    """首轮 + 口述方案 → 必须拦（此前整体豁免，实测两轮都这么走）。"""
    oral = '给你女朋友设计了一份方案：香槟色洋桔梗×5 + 紫色风信子×5，预算318元'
    assert _needs_card_nudge('送女朋友生日花，预算300左右', [], has_context=False, reply=oral)


def test_nudge_silent_on_first_turn_for_real_clarification():
    """首轮 + 老实追问 → 放行。"""
    ask = '方便告诉我送给谁、什么场合吗？预算大概多少呢？'
    assert not _needs_card_nudge('想买花', [], has_context=False, reply=ask)


def test_nudge_silent_on_first_turn_without_reply_text():
    """不传 reply 时行为与改动前一致（首轮不拦），保证既有调用点语义不变。"""
    assert not _needs_card_nudge('生日，粉色系，你决定就好', [], has_context=False)


def test_nudge_fires_on_dictated_plan_without_any_user_keywords():
    """⚠️ 关键回归（2026-09-16 实测）：「她最近心情不太好，我想让她开心一下」——
    用户话里**没有任何**花艺需求关键词（正则抽不出收花人/场合/预算/颜色），
    但模型已经口述了整套方案（含支数与价格）。此时必须拦下要卡。

    护栏判定不能只认「用户话里的关键词」，否则最自然的那类表达恰好会被漏掉。
    """
    oral = '我按这个思路配了一束：主花 向日葵4支 + 非洲菊3支，预算约150元，含人工和装饰费。'
    assert _needs_card_nudge('她最近心情不太好，我想让她开心一下', [],
                             has_context=False, reply=oral)
    # 对照：模型只是在追问时不该被拦
    assert not _needs_card_nudge('她最近心情不太好', [], has_context=False,
                                 reply='想让她开心的话，方便告诉我预算大概多少吗？')
