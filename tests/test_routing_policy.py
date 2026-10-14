"""Routing policy is validated data; v2 脊柱按理解层意图查表取策略。

v1 退役（2026-10-14）：关键词 classify 已删除，本文件只锁定策略表本身的契约——
加载校验、窄域客服工具圈、不可变性。意图 → 策略的对接契约见
tests/test_spine_understand.py（理解层白名单）与 tests/test_eval_routing_contract.py（评测集）。
"""
import json

import pytest

from agent.orchestration.policies.routing import RoutingPolicyError, load_routing_policies


def test_policies_cover_all_routable_intents():
    """理解层可路由意图必须都有策略；chitchat/meta/other 不缩圈是设计使然。"""
    from agent.engine.understand import _INTENT_DESC
    policies = load_routing_policies()
    routable = {'design', 'buying', 'qa', 'greeting', 'image', 'reminder',
                'customer_service_orders', 'customer_service_tickets',
                'customer_service_contact', 'shop_service_info', 'shop_service_reviews'}
    assert routable <= set(_INTENT_DESC)
    for name in routable:
        assert name in policies, f'意图 {name} 没有路由策略'
    assert {'chitchat', 'meta', 'other'} & set(policies) == set()


def test_policy_variants_preserve_narrow_customer_tool_scopes(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    policies = load_routing_policies()
    # 工单查询/店铺信息是窄域策略：不得携带联系商家与写操作工具
    assert 'contact_merchant' not in policies['customer_service_tickets'].tools
    assert 'contact_merchant' not in policies['shop_service_info'].tools
    assert not {'cancel_my_order', 'apply_refund', 'update_order_address'} & policies['shop_service_reviews'].tools
    # 订单售后策略携带读工具；写工具由 CUSTOMER_SERVICE_WRITE_ENABLED 门禁决定可见性
    assert {'query_my_orders', 'query_my_after_sales'} <= policies['customer_service_orders'].tools


def test_order_write_tool_is_scoped_to_trade_routes():
    """create_order 只进 buying/design 圈：客服/问答/提醒等策略不得携带下单能力。"""
    policies = load_routing_policies()
    assert 'create_order' in policies['buying'].tools
    assert 'create_order' in policies['design'].tools
    for name in ('qa', 'reminder', 'greeting', 'image', 'customer_service_orders',
                 'customer_service_tickets', 'customer_service_contact',
                 'shop_service_info', 'shop_service_reviews'):
        assert 'create_order' not in policies[name].tools, name


@pytest.mark.parametrize(('mutate', 'error'), [
    (lambda x: x + [x[0]], 'duplicate policy name'),
    (lambda x: [{**x[0], 'max_iterations': True}], 'max_iterations'),
    (lambda x: [{**x[0], 'tools': ['invented_tool']}], 'unknown tools'),
    (lambda x: [{**x[0], 'tools': ['respond_to_user', 'respond_to_user']}], 'duplicate tool'),
    (lambda x: [{**x[0], 'missing': 1}], 'expected fields'),
])
def test_policy_rejects_malformed_or_unknown_tools(tmp_path, mutate, error):
    base = [{'name': 'reminder', 'route_name': 'reminder',
             'max_iterations': 3, 'tools': ['respond_to_user']}]
    path = tmp_path / 'routing.json'
    path.write_text(json.dumps(mutate(base)), encoding='utf-8')
    with pytest.raises(RoutingPolicyError, match=error):
        load_routing_policies(path)


def test_policy_rejects_nonstandard_json_constant(tmp_path):
    path = tmp_path / 'routing.json'
    path.write_text('[{"name":"reminder","route_name":"reminder",'
                    '"max_iterations":NaN,"tools":["respond_to_user"]}]', encoding='utf-8')
    with pytest.raises(RoutingPolicyError, match='non-standard JSON constant'):
        load_routing_policies(path)


def test_policy_mapping_is_immutable():
    policies = load_routing_policies()
    with pytest.raises(TypeError):
        policies['new'] = policies['reminder']
    with pytest.raises(AttributeError):
        policies['reminder'].tools.add('contact_merchant')
