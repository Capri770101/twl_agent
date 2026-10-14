"""下单写通道（create_order）门禁回归：fail-closed 默认关闭、配置齐备才暴露/放行。

与客服写通道（tests/test_customer_tools.py）同款门禁哲学：
- 能力常驻注册表（routing.json 校验依赖注册表；执行层拒绝未开通调用）；
- 可见性双条件：PLATFORM_ORDER_ENABLED=true **且** PLATFORM_ORDER_API_URL 已配置；
- 提示词成交契约随通道状态切换（order_channel.md 注入/不注入）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.toolkit import (
    ORDER_WRITE_TAG, TOOL_REGISTRY, execute_tool, order_write_enabled,
    visible_tool_specs,
)
from backend.config import settings


@pytest.fixture
def channel_open(monkeypatch):
    """临时接通下单写通道（开关 + 真实端点配置齐备）。"""
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', True)
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_URL', 'https://api.example.com/v1/orders')
    yield


@pytest.fixture
def channel_flag_only(monkeypatch):
    """只开开关、不配端点：仍视为未接通（fail-closed）。"""
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', True)
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_URL', '')
    yield


# ── 注册与可见性 ──

def test_create_order_registered_with_write_tag():
    spec = TOOL_REGISTRY.get('create_order')
    assert spec is not None, 'create_order 必须常驻注册表（routing.json 校验依赖）'
    assert ORDER_WRITE_TAG in spec.tags


def test_create_order_hidden_by_default(monkeypatch):
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', False)
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_API_URL', '')
    assert order_write_enabled() is False
    assert not any(s.name == 'create_order' for s in visible_tool_specs())


def test_create_order_hidden_when_flag_without_url(channel_flag_only):
    """只开开关不配端点 = 未接通：不暴露，避免模型调用后白烧一轮报错。"""
    assert order_write_enabled() is False
    assert not any(s.name == 'create_order' for s in visible_tool_specs())


def test_create_order_visible_when_channel_open(channel_open):
    assert order_write_enabled() is True
    assert any(s.name == 'create_order' for s in visible_tool_specs())


# ── 执行层拦截（模型幻觉调用也挡得住）──

def test_execute_rejected_when_channel_closed(monkeypatch):
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', False)
    result, status = asyncio.run(execute_tool(
        'create_order', {'shop_id': 's1', 'plan_id': 'latest', 'plan_type': 'diy'},
        {'user_id': 'u1'}))
    assert status == 'error'
    assert '暂未开放' in result
    payload = json.loads(result) if result.startswith('{') else None
    assert payload is None or payload.get('error')


def test_execute_reaches_platform_when_channel_open(channel_open, monkeypatch):
    """通道接通后执行层放行：真正走到平台提交（这里桩掉 HTTP，验证放行路径）。"""
    import agent.skills.skill_order as SO

    async def fake_submit(payload):
        return {'order_id': 'P123', 'status': 'created', 'total_price': 199.0,
                'pay': {'page_path': '/pages/order/confirm', 'params': {'order_id': 'P123'}}}

    monkeypatch.setattr(SO, '_submit_platform_order', fake_submit)
    monkeypatch.setattr(SO, '_resolve_session_plan',
                        _async_return({'plan_id': 'P9', 'name': '测试花束', 'price': 199, 'shop_id': 's1'}))
    result, status = asyncio.run(execute_tool(
        'create_order', {'shop_id': 's1', 'plan_id': 'P9', 'plan_type': 'existing', 'source_id': 'demo'},
        {'user_id': 'u1', 'session_id': 'sid1'}))
    assert status == 'ok', result
    payload = json.loads(result)
    assert payload['order_id'] == 'P123'
    assert payload['pay_jump']['page_path']


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


# ── 路由策略：下单能力只进交易圈 ──

def test_routing_scopes_create_order_to_trade_policies():
    from agent.orchestration.policies.routing import load_routing_policies
    policies = load_routing_policies()
    assert 'create_order' in policies['buying'].tools
    assert 'create_order' in policies['design'].tools
    assert 'create_order' not in policies['qa'].tools
    assert 'create_order' not in policies['customer_service_orders'].tools


# ── 提示词成交契约随通道切换 ──
# 标记串用 order_channel.md 独有的「先确认、后下单」——base.md 的条件句里
# 也提到了「平台下单通道（已接通）」这个段名，不能拿段名当注入判据。
_ORDER_CHANNEL_MARKER = '先确认、后下单'


def test_system_prompt_default_keeps_no_order_claim(monkeypatch):
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', False)
    from agent.agent import ReActAgent
    from agent.engine.state import SessionStage
    out = ReActAgent()._build_system(SessionStage.ANALYZE, {})
    assert _ORDER_CHANNEL_MARKER not in out


def test_system_prompt_injects_order_channel_when_open(channel_open, monkeypatch):
    from agent.agent import ReActAgent
    from agent.engine.state import SessionStage
    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', '')
    out = ReActAgent()._build_system(SessionStage.ANALYZE, {})
    assert _ORDER_CHANNEL_MARKER in out
    assert 'create_order' in out


def test_demo_instance_never_gets_order_channel(channel_open, monkeypatch):
    """体验版（店铺链路关闭）即使配置了下单通道也不注入成交话术。"""
    from agent.agent import ReActAgent
    from agent.engine.state import SessionStage
    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', 'plan')
    out = ReActAgent()._build_system(SessionStage.ANALYZE, {})
    assert _ORDER_CHANNEL_MARKER not in out


# ── 交易护栏与通道口径一致 ──

def test_trade_guard_allows_claim_only_with_tool_evidence(channel_open):
    from agent.engine.ui_protocol import ToolCallRecord
    from agent.safety.trade_claim import _needs_trade_nudge
    claimed = '已经为你下单成功，订单号 P123。'
    evidence = ToolCallRecord(name='create_order', arguments={}, status='ok',
                              result='{"order_id":"P123","status":"created"}', source='model')
    assert _needs_trade_nudge([], claimed) is True, '无工具证据不得声称已下单'
    assert _needs_trade_nudge([evidence], claimed) is False, '有成功下单证据应放行'


def test_trade_nudge_text_adapts_to_channel(channel_open, monkeypatch):
    from agent.safety.trade_claim import _trade_nudge_text
    text_open = _trade_nudge_text()
    assert 'create_order' in text_open
    monkeypatch.setattr(settings, 'PLATFORM_ORDER_ENABLED', False)
    text_closed = _trade_nudge_text()
    assert '不支持代客下单' in text_closed
