import asyncio
import json

from agent.greeting_context import plan_context
from agent.skills import skill_greeting as greeting


def test_plan_context_extracts_card_facts():
    out = plan_context({'name': '生日花束', 'recipient': '女朋友', 'occasion': '生日', 'style': '温柔', 'card_message': '愿你开心', 'design': {'main_flowers': [{'name': '玫瑰', 'qty': 11}], 'color_scheme': ['白色', '绿色'], 'meaning': '长久陪伴'}})
    assert out['recipient'] == '女朋友'
    assert out['flowers'] == '玫瑰×11'
    assert out['colors'] == '白色、绿色'


def test_suggest_uses_current_plan_context(monkeypatch):
    async def fake(ctx):
        return {'recipient': '女朋友', 'occasion': '生日', 'style': '深情'}
    monkeypatch.setattr('agent.greeting_context.current_greeting_context', fake)
    out = json.loads(asyncio.run(greeting.suggest_greetings(_context={'user_id': 'u', 'session_id': 's'})))
    assert out['recipient_input'] == '女朋友'
    assert out['occasion_input'] == '生日'
    assert out['plan_context']['style'] == '深情'
