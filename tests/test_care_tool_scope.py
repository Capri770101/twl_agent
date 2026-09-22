import asyncio
import pytest
from agent.engine.tool_scope import is_standalone_care, scoped_tools, active_tools, active_entities, CARE_TOOLS
from agent.toolkit import to_openai_tools, execute_tool
from backend.config import settings


@pytest.mark.parametrize('text', ['玫瑰怎么养得久？只讲养护，不要方案和图片。', '鲜花怎么换水', '如何醒花'])
def test_care(text):
    assert is_standalone_care(text)


@pytest.mark.parametrize('text', ['帮我设计方案并讲养护', '推荐好养的花', '这家店有保鲜剂吗', '给妈妈买花怎么养', '改成白色再说怎么养', '不要生图，帮我设计', '这个多少钱', '记住我不喜欢百合，怎么养玫瑰'])
def test_mixed_requests_keep_full_tools(text):
    assert not is_standalone_care(text)


def test_concurrent_scope_and_execution_boundary(monkeypatch):
    monkeypatch.setattr(settings, 'CARE_TOOL_SCOPE_ENABLED', True)
    @scoped_tools
    async def run(self, user_id, message):
        await asyncio.sleep(0)
        names = {t['function']['name'] for t in to_openai_tools()}
        if '养护' in message:
            assert names == CARE_TOOLS
            assert (await execute_tool('generate_effect_image', {}))[1] == 'error'
        else:
            assert 'generate_diy_plan' in names
    async def scenario():
        await asyncio.gather(run(None, 'a', '玫瑰养护'), run(None, 'b', '设计一束花'))
        assert active_tools.get() is None
    asyncio.run(scenario())


def test_disabled_by_default_path(monkeypatch):
    monkeypatch.setattr(settings, 'CARE_TOOL_SCOPE_ENABLED', False)
    @scoped_tools
    async def run(self, user_id, message):
        assert active_tools.get() is None
    asyncio.run(run(None, 'u', '玫瑰养护'))


def test_buying_route_limits_platform_entity(monkeypatch):
    monkeypatch.setattr(settings, 'AGENT_INTENT_ROUTING_ENABLED', True)
    @scoped_tools
    async def run(self, user_id, message):
        assert active_entities.get() == frozenset({'plan'})
        spec = next(t for t in to_openai_tools() if t['function']['name'] == 'platform_db_query_entity')
        assert spec['function']['parameters']['properties']['entity']['enum'] == ['plan']
    asyncio.run(run(None, 'u', '预算200送妈妈，推荐一束好养的花'))


def test_disjoint_entity_scope_denies_all(monkeypatch):
    from agent.toolkit import allowed_entities
    from agent.data_tools import platform_db_query_entity
    import json
    monkeypatch.setattr(settings, 'PLATFORM_ALLOWED_ENTITIES', 'shop')
    token = active_entities.set(frozenset({'plan'}))
    try:
        assert allowed_entities() == {'__deny_all__'}
        assert all(t['function']['name'] != 'platform_db_query_entity' for t in to_openai_tools())
        assert json.loads(platform_db_query_entity('unused', 'shop'))['ok'] is False
    finally:
        active_entities.reset(token)
