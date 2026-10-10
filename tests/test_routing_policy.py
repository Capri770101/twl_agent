"""Routing policy is validated data; fast classification remains behavior compatible."""
import json

import pytest

from agent.engine.intent import classify
from agent.orchestration.policies.routing import RoutingPolicyError, load_routing_policies


@pytest.mark.parametrize(('message', 'name', 'iterations', 'tools'), [
    ('明天提醒我换水', 'reminder', 3, {'set_reminder', 'manage_my_reminders'}),
    ('给这个方案生成一张效果图', 'image', 2, {'generate_effect_image'}),
    ('帮我写一段祝福', 'greeting', 3, {'suggest_greetings'}),
    ('定制一束花', 'design', 4, {'generate_diy_plan'}),
    ('玫瑰怎么养', 'qa', 2, {'retrieve_knowledge'}),
    ('推荐花束', 'buying', 3, {'platform_db_query_entity'}),
])
def test_fast_route_uses_declared_policy(message, name, iterations, tools):
    route = classify(message)
    assert route is not None and route.name == name
    assert route.max_iterations == iterations
    assert tools <= route.tools
    assert route.tools == load_routing_policies()[name].tools


def test_policy_variants_preserve_narrow_customer_tool_scopes(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    policies = load_routing_policies()
    for message, key in [('我的订单到哪了', 'customer_service_orders'),
                         ('我的工单进度', 'customer_service_tickets'),
                         ('花店评价怎么样', 'shop_service_reviews'),
                         ('联系商家', 'customer_service_contact'),
                         ('店铺营业时间', 'shop_service_info')]:
        route = classify(message)
        policy = policies[key]
        assert route.name == policy.route_name
        assert route.tools == policy.tools
        assert route.max_iterations == policy.max_iterations
    assert 'contact_merchant' not in classify('我的工单进度').tools
    assert 'contact_merchant' not in classify('店铺营业时间').tools


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
