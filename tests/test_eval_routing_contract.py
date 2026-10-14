"""Declared scenarios must not drift from the v2 routing policy table.

v1 退役（2026-10-14）：意图不再由关键词 classify 判定，而是模型在理解层声明后
查 routing.json 取策略。评测场景声明的 intent 是**对外契约的 route_name**
（customer_service/shop_service 各对应多个窄域策略变体），本文件锁定：
- 场景 intent 必须能映射到至少一个策略（route_name 相同）；
- 场景声明的 max_iterations 必须是该组策略之一的上限；
- allowed_tools 必须被该组策略的工具并集覆盖；
- forbidden_tools 不得出现在该组任何策略的工具圈里。
"""
import json

import pytest

from agent.orchestration.policies.routing import load_routing_policies
from scripts.validate_eval_set import DEFAULT_CASES


def _policies_by_route() -> dict[str, list]:
    groups: dict[str, list] = {}
    for policy in load_routing_policies().values():
        groups.setdefault(policy.route_name, []).append(policy)
    return groups


@pytest.mark.parametrize('scenario', [json.loads(line) for line in DEFAULT_CASES.read_text(encoding='utf-8').splitlines() if line.strip()], ids=lambda c: c['id'])
def test_eval_case_matches_declared_policy_contract(scenario):
    groups = _policies_by_route()
    intent = scenario['intent']
    assert intent in groups, f"{scenario['id']}: intent {intent!r} 没有对应路由策略"
    group = groups[intent]
    assert any(p.max_iterations == scenario['max_iterations'] for p in group), \
        f"{scenario['id']}: max_iterations 与策略组 {[p.max_iterations for p in group]} 不符"
    union = set().union(*(p.tools for p in group))
    assert set(scenario['allowed_tools']) <= union, \
        f"{scenario['id']}: allowed_tools 超出策略组工具并集"
    assert not set(scenario['forbidden_tools']) & union, \
        f"{scenario['id']}: forbidden_tools 出现在策略组工具圈"
