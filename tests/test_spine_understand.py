"""v2 脊柱：理解层契约与路由查表。"""
from __future__ import annotations

from agent.engine.understand import _validate
from agent.orchestration.policies.routing import load_routing_policies
from agent.spine import _POLICIES


def test_validate_rejects_non_dict_and_bad_intent():
    assert _validate('not-a-dict') is None
    u = _validate({'intent': '不存在', 'requirement': {'recipient': '妈妈'}})
    assert u is not None and u.intent is None  # 非法意图置空，槽位保留给白名单合并


def test_validate_whitelists_intent_and_fields():
    u = _validate({'intent': 'design', 'requirement': {'gift_intent': 'apology'},
                   'needs_platform': 'plan', 'plan_discussion': True, 'missing': ['budget']})
    assert u.intent == 'design' and u.needs_platform == 'plan' and u.plan_discussion is True
    assert u.missing == ['budget']


def test_validate_normalizes_bad_needs_platform_and_missing():
    u = _validate({'intent': 'qa', 'needs_platform': 'user', 'missing': 'budget'})
    assert u.needs_platform == '' and u.missing == []


def test_spine_route_table_uses_declared_policies():
    policies = load_routing_policies()
    assert _POLICIES is policies or set(_POLICIES) == set(policies)
    # 理解层枚举里的可路由意图必须都能在策略表里查到
    from agent.engine.understand import _INTENT_DESC
    routable = {'design', 'buying', 'qa', 'greeting', 'image', 'reminder',
                'customer_service_orders', 'customer_service_tickets',
                'customer_service_contact', 'shop_service_info', 'shop_service_reviews'}
    assert routable <= set(_INTENT_DESC)
    for name in routable:
        assert name in _POLICIES, f'意图 {name} 没有路由策略'
    # chitchat/meta/other 是「不缩圈」意图，不在路由表是设计使然
    assert {'chitchat', 'meta', 'other'} & set(_POLICIES) == set()
