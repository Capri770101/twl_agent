"""工具注册表与「ops 隔离」的回归测试。

为什么重要
----------
1. 功能性工具是智能体的能力边界 —— 任何一个丢失都会导致某类需求无法满足（用户明确要求"功能工具一个不能丢"）。
2. 8 个平台接入/运维工具（含能改映射状态的 platform_mapping_set_status）**不应暴露给 C 端会话**，
   这是权限面收窄的关键；同时要保证接入期可临时开启。
"""
from __future__ import annotations

import asyncio

import pytest

from agent.toolkit import (
    OPS_TOOL_TAG,
    TOOL_REGISTRY,
    execute_tool,
    to_openai_tools,
    visible_tool_specs,
)

# C 端必须始终可用的功能性工具
FUNCTIONAL_TOOLS = {
    'platform_db_query_entity',
    'generate_diy_plan',
    'revise_diy_plan',
    'generate_effect_image',
    'get_user_profile',
    'save_user_profile',
    'save_memory',
    'search_history',
    'retrieve_knowledge',
    'respond_to_user',
    'show_plan_card',
    'suggest_greetings',
    'render_greeting_card',
}

# 运维 / 接入期工具（默认对 C 端隐藏）
OPS_TOOLS = {
    'platform_db_discover',
    'platform_db_test_connection',
    'platform_db_sample_table',
    'platform_mapping_draft',
    'platform_mapping_save_draft',
    'platform_mapping_list_drafts',
    'platform_mapping_set_status',
    'platform_mapping_get_active',
}


def test_registry_not_empty() -> None:
    assert TOOL_REGISTRY, '工具注册表为空 —— 检查 agent/__init__.py 的导入触发'


def test_functional_tools_all_registered() -> None:
    missing = FUNCTIONAL_TOOLS - set(TOOL_REGISTRY)
    assert not missing, f'功能性工具丢失：{sorted(missing)}'


def test_ops_tools_all_registered_and_tagged() -> None:
    for name in OPS_TOOLS:
        assert name in TOOL_REGISTRY, f'运维工具未注册：{name}'
        assert OPS_TOOL_TAG in TOOL_REGISTRY[name].tags, f'{name} 缺少 ops 标签'


def test_ops_hidden_by_default(ops_disabled) -> None:
    visible = {spec.name for spec in visible_tool_specs()}
    assert FUNCTIONAL_TOOLS <= visible, '功能性工具被误过滤'
    assert not (OPS_TOOLS & visible), f'运维工具泄露到 C 端：{sorted(OPS_TOOLS & visible)}'


def test_ops_visible_when_enabled(ops_enabled) -> None:
    """部署接入期可临时开启运维工具。"""
    visible = {spec.name for spec in visible_tool_specs()}
    assert OPS_TOOLS <= visible


def test_to_openai_tools_matches_visible(ops_disabled) -> None:
    names = {tool['function']['name'] for tool in to_openai_tools()}
    assert names == {spec.name for spec in visible_tool_specs()}
    assert not (OPS_TOOLS & names)


def test_execute_tool_rejects_hidden_ops(ops_disabled) -> None:
    """即使模型幻觉调用，未开放的运维工具也必须被拒绝。"""
    result, status = asyncio.run(execute_tool('platform_db_discover', {'source_id': 'x'}, {}))
    assert status == 'error'
    assert '不可用' in result


def test_execute_tool_unknown_name(ops_disabled) -> None:
    result, status = asyncio.run(execute_tool('no_such_tool_exists', {}, {}))
    assert status == 'error'
    assert '未知工具' in result


def test_functional_tool_executable(ops_disabled) -> None:
    """功能性工具不受 ops 开关影响，始终可调用。"""
    result, status = asyncio.run(execute_tool('respond_to_user', {'reply': '你好'}, {}))
    assert status == 'ok'
