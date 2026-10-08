import asyncio
import pytest
from agent.engine.discussion import is_plan_discussion
from agent.engine.tool_scope import scoped_tools, active_tools
from agent.agent import _needs_card_nudge


@pytest.mark.parametrize('message', ['这个手工费怎么算的？', '这个粉白配色会不会太素？', '换成白色会不会更好？', '先别改方案，我们先聊聊包装', '为什么用13支康乃馨？'])
def test_discussion_does_not_force_card_or_mutating_tools(message):
    assert is_plan_discussion(message)
    assert not _needs_card_nudge(message, [], True, '当前方案是康乃馨13支，参考价193元。')
    @scoped_tools
    async def run(self, uid, message):
        assert 'generate_diy_plan' not in active_tools.get()
        assert 'platform_db_query_entity' not in active_tools.get()
        assert 'generate_effect_image' not in active_tools.get()
    asyncio.run(run(None, 'u', message))
    assert active_tools.get() is None


@pytest.mark.parametrize('message', ['请改成白色', '帮我重新设计一束花', '直接换成玫瑰', '为什么这么贵？请帮我改成100元的方案', '现在生成效果图'])
def test_explicit_action_not_blocked(message):
    assert not is_plan_discussion(message)
