"""会话级需求记忆（跨轮槽位累积）回归门。

修复的真实缺陷：需求每轮只从**当前这条消息**抽取。用户分多轮补充
（「送妈妈」→「生日」→「预算200」）时，前面说过的信息进不了结构化需求——
LLM 靠对话历史还记得，但规则引擎拿不到，于是「LLM 失败回退 baseline」时方案缺信息。
"""

from __future__ import annotations

import inspect

from agent.agent import _clarify_slots, _has_any_requirement, UIType
from agent.tools import design_diy_plan, design_with_llm, extract_requirement
from domain.requirements import FlowerRequirement, accumulate

CARD = UIType.PLAN_CARD


# ── 1. accumulate：跨轮累积（补充不丢、改口生效）──

def test_accumulate_first_turn() -> None:
    acc = accumulate(None, extract_requirement('送给妈妈的花，预算200'))
    assert acc.recipient == '母亲' and acc.budget_num == 200


def test_accumulate_cross_turn_keeps_earlier_slots() -> None:
    """核心场景：第 1 轮说送谁，第 2 轮说预算 → 合并后两者都在。"""
    t1 = extract_requirement('送给妈妈一束花')
    t2 = extract_requirement('预算200')
    acc = accumulate(t1, t2)
    assert acc.recipient == '母亲'
    assert acc.budget_num == 200
    # 再补一轮场合
    acc2 = accumulate(acc, extract_requirement('是生日'))
    assert acc2.recipient == '母亲' and acc2.budget_num == 200 and acc2.occasion == '生日'


def test_accumulate_later_value_wins() -> None:
    """用户改口要生效：后说的覆盖先说的。"""
    acc = accumulate(extract_requirement('预算200'), extract_requirement('预算500'))
    assert acc.budget_num == 500


def test_accumulate_colors_follow_current_turn() -> None:
    """colors 特殊：跨轮取「本轮为准」，否则「换成粉色」后旧的红会残留。"""
    prev = extract_requirement('要红玫瑰')
    cur = extract_requirement('换成粉色')
    assert prev.colors == ['红'] and cur.colors == ['粉']
    assert accumulate(prev, cur).colors == ['粉']
    # 本轮没提颜色 → 保留旧颜色
    assert accumulate(prev, extract_requirement('预算300')).colors == ['红']


def test_accumulate_hard_constraints_and_location() -> None:
    acc = accumulate(extract_requirement('就要纯白百合'), extract_requirement('十一朵'))
    assert acc.single_flower == '百合' and acc.stem_count == 11
    loc = {'lat': 22.5, 'lng': 114.0}
    assert accumulate(FlowerRequirement(location=loc), FlowerRequirement()).location == loc


def test_accumulate_is_pure_and_none_safe() -> None:
    prev = extract_requirement('送给妈妈一束花')
    snapshot = prev.to_dict()
    accumulate(prev, extract_requirement('预算200'))
    assert prev.to_dict() == snapshot, 'accumulate 不得修改入参'
    empty = accumulate(None, None)
    assert isinstance(empty, FlowerRequirement) and not _has_any_requirement(empty)


# ── 2. _has_any_requirement：需求强度判据 ──

def test_has_any_requirement() -> None:
    assert _has_any_requirement(None) is False
    assert _has_any_requirement(FlowerRequirement()) is False
    assert _has_any_requirement(extract_requirement('预算200')) is True
    assert _has_any_requirement(extract_requirement('要粉色的')) is True
    assert _has_any_requirement(extract_requirement('送妈妈')) is True


# ── 3. L3 追问兜底：模型漏报 missing 时用累积需求兜住 ──

def test_clarify_fallback_when_model_reports_nothing() -> None:
    # 模型没填 missing，且会话累积需求一项信号都没有 → 仍然追问最关键的两项
    slots = _clarify_slots('给我设计一束花', {}, CARD, True, False, session_req=FlowerRequirement())
    assert slots == ['recipient', 'occasion']


def test_clarify_no_fallback_when_requirement_has_signal() -> None:
    for text in ('送妈妈', '预算200', '要粉色的', '生日'):
        req = extract_requirement(text)
        assert _clarify_slots('给我设计一束花', {}, CARD, True, False, session_req=req) == [], f'「{text}」已给信息却仍追问'


def test_clarify_model_report_takes_priority() -> None:
    # 模型自报的槽位优先，不被兜底覆盖
    slots = _clarify_slots('看看方案', {'missing': ['budget']}, CARD, True, False, session_req=FlowerRequirement())
    assert slots == ['budget']


def test_clarify_keeps_old_behavior_without_session_req() -> None:
    assert _clarify_slots('给我设计一束花', {}, CARD, True, False) == []
    # 豁免词 / 非卡片 / 非 DIY 路径仍一律不追问
    assert _clarify_slots('随便你决定', {}, CARD, True, False, session_req=FlowerRequirement()) == []
    assert _clarify_slots('给我设计一束花', {}, UIType.TEXT, True, False, session_req=FlowerRequirement()) == []
    assert _clarify_slots('给我设计一束花', {}, CARD, False, False, session_req=FlowerRequirement()) == []


# ── 4. 契约：设计入口必须能接收会话累积需求 ──

def test_design_entries_accept_session_requirement() -> None:
    assert 'session_requirement' in inspect.signature(design_with_llm).parameters
    assert 'session_requirement' in inspect.signature(design_diy_plan).parameters
