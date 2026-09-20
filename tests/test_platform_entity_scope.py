"""平台数据「可查实体」白名单回归门（体验/演示实例只做方案与建议）。

背景（2026-09-16，Capri 决定）：体验页接入真实数据，但**只要「生成方案 + 给建议」**这条
主线——店铺推荐与下单链路不要。于是用 ``PLATFORM_ALLOWED_ENTITIES`` 把范围收窄：

1. **schema 层**：``visible_tool_specs()`` 把 ``platform_db_query_entity`` 的 ``entity``
   收窄（模型看不到 ``shop``，就不会去查店铺，也不会产出店铺卡或「去哪家店买」的话术）；
2. **执行层**：工具自身再挡一层（防模型照提示词/历史硬传）；
3. **prompt 层**：换成 ``full_platform_plan_only`` 变体（由 test_prompt.py 保护）；
4. **卡片层**：``_shop_entity_enabled()`` 为假时不产店铺卡。

留空 = 不限制（生产默认行为完全不变）。
"""
from __future__ import annotations

import json

from agent import toolkit
from agent.agent import _shop_entity_enabled
from agent.data_tools import platform_db_query_entity


def _set_allowed(monkeypatch, value: str) -> None:
    from backend.config import settings

    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', value, raising=False)


def _entity_spec() -> toolkit.ToolSpec:
    return next(s for s in toolkit.visible_tool_specs() if s.name == 'platform_db_query_entity')


# ── allowed_entities 解析 ────────────────────────────────────────────

def test_empty_means_unrestricted(monkeypatch):
    _set_allowed(monkeypatch, '')
    assert toolkit.allowed_entities() == set()


def test_parses_halfwidth_and_fullwidth_commas(monkeypatch):
    _set_allowed(monkeypatch, 'plan， shop ,')
    assert toolkit.allowed_entities() == {'plan', 'shop'}


def test_case_insensitive(monkeypatch):
    _set_allowed(monkeypatch, 'PLAN')
    assert toolkit.allowed_entities() == {'plan'}


# ── schema 收窄 ──────────────────────────────────────────────────────

def test_narrows_entity_enum_and_description(monkeypatch):
    _set_allowed(monkeypatch, 'plan')
    ent = _entity_spec().parameters['properties']['entity']
    assert ent['enum'] == ['plan']
    assert ent['description'] == 'plan'


def test_removes_shop_from_tool_description(monkeypatch):
    """描述里的「entity 取值：…」枚举串也要收窄，否则与 schema 自相矛盾。"""
    _set_allowed(monkeypatch, 'plan')
    desc = _entity_spec().description
    assert 'shop' not in desc
    assert 'entity 取值：plan；' in desc


def test_unrestricted_keeps_original_schema(monkeypatch):
    _set_allowed(monkeypatch, '')
    ent = _entity_spec().parameters['properties']['entity']
    assert ent['enum'] == ['plan', 'shop']
    assert 'shop' in _entity_spec().description
    assert 'shop' in ent['description']


def test_registry_not_mutated(monkeypatch):
    """收窄必须作用在副本上——不能污染 TOOL_REGISTRY 里的原始定义。"""
    _set_allowed(monkeypatch, 'plan')
    toolkit.visible_tool_specs()
    original = next(s for s in toolkit.get_tool_specs() if s.name == 'platform_db_query_entity')
    assert 'enum' not in original.parameters['properties']['entity']
    assert 'shop' in original.description


def test_invalid_allowlist_hides_query_tool(monkeypatch):
    """白名单写错（没有任何合法实体）时必须 fail-closed：下架查询工具，
    **不能**回退成「不限制」——那会让体验实例的收窄静默失效。"""
    _set_allowed(monkeypatch, 'none')
    assert all(s.name != 'platform_db_query_entity' for s in toolkit.visible_tool_specs())


# ── 执行层兜底 ───────────────────────────────────────────────────────

def test_query_rejects_disallowed_entity(monkeypatch):
    """模型硬传 shop 时直接拒绝，且**不会**发起真实查询。"""
    _set_allowed(monkeypatch, 'plan')
    out = json.loads(platform_db_query_entity('aistore', 'shop'))
    assert out.get('ok') is False
    assert '不提供' in str(out.get('error'))


def test_query_allows_whitelisted_entity(monkeypatch):
    """白名单内的实体照常放行（这里只验不被前置拦截，实际查询由数据源决定）。"""
    _set_allowed(monkeypatch, 'plan')
    out = json.loads(platform_db_query_entity('__not_configured__', 'plan'))
    assert '不提供该实体' not in json.dumps(out, ensure_ascii=False)


# ── 店铺链路开关 ─────────────────────────────────────────────────────

def test_shop_entity_enabled_matrix(monkeypatch):
    _set_allowed(monkeypatch, '')
    assert _shop_entity_enabled() is True
    _set_allowed(monkeypatch, 'plan')
    assert _shop_entity_enabled() is False
    _set_allowed(monkeypatch, 'plan,shop')
    assert _shop_entity_enabled() is True


# ── 体验版交易引导兜底（实测：「点击卡片即可在小程序下单配送」）──────

def test_trade_note_appended_when_cta_present():
    from agent.agent import _demo_trade_note

    out = _demo_trade_note('都远低于300，点击卡片即可在小程序下单配送。')
    assert '体验版仅作参考展示' in out
    assert '下单' in out  # 原文不被改写，只在末尾补说明（避免破坏语感）


def test_trade_note_silent_without_cta():
    from agent.agent import _demo_trade_note

    text = '给你挑了 3 款，点击卡片可以看详情。'
    assert _demo_trade_note(text) is text


def test_trade_note_not_duplicated():
    from agent.agent import _demo_trade_note

    text = '体验版仅作参考展示，选购与下单请到正式小程序。'
    assert _demo_trade_note(text) is text


def test_trade_note_handles_empty():
    from agent.agent import _demo_trade_note

    assert _demo_trade_note('') == ''
    assert _demo_trade_note(None) is None
