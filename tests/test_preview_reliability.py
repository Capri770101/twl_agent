"""真实任务证据、无最终工具回复分支和 DIY 方案归属的回归门。"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import agent.agent as module
import agent.tools as tools
from agent.agent import ReActAgent, SessionStage
from agent.engine.ui_protocol import ToolCallRecord, UIType


def record(name, data):
    return ToolCallRecord(name=name, arguments={}, result=json.dumps(data), status='ok')


@pytest.mark.parametrize('respond', [None, {'ui': 'plan_card', 'data': {}, 'reply': '方案完成'}])
@pytest.mark.parametrize('image_result', [{'error': 'upstream failed'}, {'task_id': 'existing'}])
def test_plan_always_exposes_real_preview_task(monkeypatch, respond, image_result):
    for method in ('get_session_flag', 'set_session_flag', 'clear_session_flags'):
        monkeypatch.setattr(module.mem_store, method, AsyncMock(return_value=None))
    monkeypatch.setattr(module, '_clarify_slots', lambda *a, **k: [])
    generate = AsyncMock(return_value=json.dumps({'task_id': 'recovered', 'poll': '/tasks/recovered'}))
    monkeypatch.setattr(tools, 'generate_effect_image', generate)
    log = [record('generate_diy_plan', {'plan_id': 'DIY_1', 'diy': True, 'name': '方案'}),
           record('generate_effect_image', image_result)]
    _, ui, data, _, _ = asyncio.run(ReActAgent()._post_process(
        respond, log, SessionStage.DIY_DESIGN, '设计花束', '方案完成', 'u', 's', None, []))
    assert ui == UIType.PLAN_CARD
    expected = 'existing' if image_result.get('task_id') else 'recovered'
    assert data['task_id'] == expected
    assert data['poll'] == '/tasks/' + expected
    assert generate.await_count == (0 if expected == 'existing' else 1)


def test_latest_diy_does_not_use_selected_product(monkeypatch):
    async def get(uid, sid, key):
        return {'name': 'DIY'} if key == 'latest_diy_plan' else {'name': '旧商品'}
    monkeypatch.setattr(tools.memory, 'get_session_json', get)
    assert asyncio.run(tools._resolve_session_plan('latest_diy', {'user_id': 'u', 'session_id': 's'})) == {'name': 'DIY'}
