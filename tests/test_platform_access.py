"""验证授权在 I/O 之前生效、异步/线程传播及同店铺缓存隔离。"""
import asyncio
import json

import pytest

from backend.config import settings
from backend.data_gateway import access, external


@pytest.fixture
def tenants(monkeypatch):
    for key in list(access.os.environ):
        if key.startswith(('PLATFORM_DB_', 'PLATFORM_API_')) and key.endswith('_URL'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('PLATFORM_API_ALPHA_URL', 'https://alpha.invalid')
    monkeypatch.setenv('PLATFORM_API_BETA_URL', 'https://beta.invalid')
    monkeypatch.setattr(settings, 'PLATFORM_SOURCE_ACCESS', json.dumps({'a': ['alpha'], 'b': ['beta']}))
    monkeypatch.setattr(settings, 'PLATFORM_SINGLE_SOURCE_COMPAT', False)


def test_authorized_source_only(tenants):
    with access.platform_scope('a'):
        assert access.visible_sources() == ['alpha']
        access.require_public_entity('alpha', 'plan')
        with pytest.raises(PermissionError):
            external.query_external_entity('beta', 'plan')
        for entity in ('user', 'order'):
            with pytest.raises(PermissionError):
                external.query_external_entity('alpha', entity)


@pytest.mark.parametrize('raw', ['', 'broken', '[]', '{"a":"alpha"}', '{"a":[3]}'])
def test_invalid_mapping_fails_closed(tenants, monkeypatch, raw):
    monkeypatch.setattr(settings, 'PLATFORM_SOURCE_ACCESS', raw)
    assert access.sources_for_platform('a') == frozenset()


def test_unknown_and_missing_identity_denied(tenants):
    assert not access.sources_for_platform('unknown')
    assert not access.sources_for_platform(None)


def test_single_source_compat_requires_opt_in(tenants, monkeypatch):
    monkeypatch.setattr(settings, 'PLATFORM_SOURCE_ACCESS', '')
    monkeypatch.delenv('PLATFORM_API_BETA_URL')
    assert not access.sources_for_platform(None)
    monkeypatch.setattr(settings, 'PLATFORM_SINGLE_SOURCE_COMPAT', True)
    assert access.sources_for_platform(None) == frozenset({'alpha'})
    monkeypatch.setenv('PLATFORM_API_BETA_URL', 'https://beta.invalid')
    assert not access.sources_for_platform(None)


def test_concurrent_scopes_propagate_to_threads_and_reset(tenants):
    @access.scoped_agent_run
    async def run():
        await asyncio.sleep(0)
        return await asyncio.to_thread(access.visible_sources)

    async def scenario():
        assert await asyncio.gather(run(platform_id='a'), run(platform_id='b')) == [['alpha'], ['beta']]
        assert access.cache_scope() is None
    asyncio.run(scenario())


def test_same_shop_material_cache_isolated(tenants, monkeypatch):
    from agent import shop_materials
    shop_materials.clear_cache()
    monkeypatch.setattr(shop_materials, '_fetch', lambda shop: set(access.visible_sources()))
    try:
        with access.platform_scope('a'):
            assert shop_materials.available_materials('same') == {'alpha'}
        with access.platform_scope('b'):
            assert shop_materials.available_materials('same') == {'beta'}
        shop_materials.clear_cache('same')
        assert not shop_materials._CACHE
    finally:
        shop_materials.clear_cache()


def test_same_shop_price_cache_isolated(tenants, monkeypatch):
    from agent import pricing
    pricing.clear_cache()
    monkeypatch.setattr(pricing, '_fetch_rows', lambda shop: access.visible_sources())
    monkeypatch.setattr(pricing, 'fit_from_rows', lambda rows, shop: rows[0])
    try:
        with access.platform_scope('a'):
            assert pricing._fit_cached('same') == 'alpha'
        with access.platform_scope('b'):
            assert pricing._fit_cached('same') == 'beta'
        pricing.clear_cache('same')
        assert not pricing._CACHE
    finally:
        pricing.clear_cache()


def test_ops_unavailable_in_customer_run_even_if_enabled(tenants, monkeypatch):
    from agent import toolkit
    monkeypatch.setattr(settings, 'ENABLE_OPS_TOOLS', True)
    with access.platform_scope('a'):
        assert all('ops' not in spec.tags for spec in toolkit.visible_tool_specs())
        _, status = asyncio.run(toolkit.execute_tool('platform_mapping_set_status', {}))
        assert status == 'error'


@pytest.mark.parametrize('columns,shop,row_id', [({'name': 'name'}, 's', ''), ({'name': 'name'}, '', 'p')])
def test_missing_scope_column_rejected(tenants, monkeypatch, columns, shop, row_id):
    from contextlib import contextmanager
    from backend.data_gateway import mapping_store, http_source
    monkeypatch.setattr(http_source, 'is_configured', lambda source: False)
    monkeypatch.setattr(mapping_store, 'get_active_mapping', lambda source: {
        'draft_json': {'entities': {'plan': {'selected': {'table': 'products', 'columns': columns}}}}
    })
    monkeypatch.setattr(external, '_source_url', lambda source: 'postgresql://unused')
    monkeypatch.setattr(external, '_resolve_schema', lambda *args: 'public')

    class NoQuery:
        def execute(self, *args):
            pytest.fail('unscoped SQL must not execute')

    @contextmanager
    def connection(source):
        yield NoQuery()

    monkeypatch.setattr(external, '_connect_external', connection)
    with access.platform_scope('a'), pytest.raises(PermissionError):
        external.query_external_entity('alpha', 'plan', shop_id=shop, row_id=row_id)


@pytest.mark.parametrize('streaming', [False, True])
def test_routes_forward_authenticated_platform(monkeypatch, streaming):
    from types import SimpleNamespace
    from backend.routers import chat
    from backend.auth import TokenPayload
    from unittest.mock import AsyncMock

    monkeypatch.setattr(settings, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(chat, '_enforce_rate_limit', lambda *args: None)
    monkeypatch.setattr(chat, 'record_call_start', lambda *args: None)
    monkeypatch.setattr(chat, 'record_call_end', lambda *args, **kwargs: None)
    monkeypatch.setattr(chat, '_spawn_consolidate', lambda *args: None)
    monkeypatch.setattr(chat, '_AGENT_SEM', asyncio.Semaphore(1))
    monkeypatch.setattr(chat.mem_store, 'get_conversation', AsyncMock(return_value={'user_id': 'u'}))
    monkeypatch.setattr(chat.mem_store, 'update_conversation_preview', AsyncMock())
    seen = []

    class Agent:
        async def arun(self, *args, **kwargs):
            seen.append(kwargs['platform_id'])
            return SimpleNamespace(session_id='s', tool_calls=[], model_dump=lambda: {})

        async def arun_stream(self, *args, **kwargs):
            seen.append(kwargs['platform_id'])
            yield {'event': 'done'}

    monkeypatch.setattr(chat, 'get_agent', lambda: Agent())

    async def scenario():
        req = chat.ChatRequest(message='查商品', user_id='u', session_id='s')
        route = chat.chat_stream if streaming else chat.chat
        result = await route(req, None, authenticated_user='u', user_info=TokenPayload('u', 'verified'))
        if streaming:
            async for _ in result.body_iterator:
                pass
        assert seen == ['verified']
    asyncio.run(scenario())
