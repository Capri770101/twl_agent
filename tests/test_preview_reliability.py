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


def test_explicit_plan_id_uses_exact_session_snapshot(monkeypatch):
    async def get(uid, sid, key):
        assert (uid, sid) == ('u', 's')
        if key == 'latest_diy_plan':
            return {'plan_id': 'DIY_new', 'name': '新版'}
        return [{'plan_id': 'DIY_old', 'name': '旧版'}]
    monkeypatch.setattr(tools.memory, 'get_session_json', get)
    ctx = {'user_id':'u', 'session_id':'s'}
    assert asyncio.run(tools._resolve_session_plan('DIY_old', ctx))['name'] == '旧版'
    assert asyncio.run(tools._resolve_session_plan('DIY_other_user', ctx)) is None
    assert asyncio.run(tools._resolve_session_plan('DIY_new', ctx))['name'] == '新版'


@pytest.mark.parametrize('reply_ui,reply_data', [('text', {'summary':'方案完成'}), ('dialog_options', {'options':[{'label':'保存','value':'保存'}]}), ('image_task', {'task_id':'model-invented'})])
def test_real_diy_overrides_non_plan_ui_and_submits_preview(monkeypatch, reply_ui, reply_data):
    for method in ('get_session_flag', 'set_session_flag', 'clear_session_flags'):
        monkeypatch.setattr(module.mem_store, method, AsyncMock(return_value=None))
    monkeypatch.setattr(module, '_clarify_slots', lambda *a, **k: [])
    generate = AsyncMock(return_value=json.dumps({'task_id':'real-task','poll':'/tasks/real-task'}))
    monkeypatch.setattr(tools, 'generate_effect_image', generate)
    log = [record('generate_diy_plan', {'plan_id':'DIY_valid', 'diy':True, 'name':'真实方案'})]
    _, ui, data, _, _ = asyncio.run(ReActAgent()._post_process(
        {'ui':reply_ui,'data':reply_data,'reply':'方案完成'}, log, SessionStage.DIY_DESIGN,
        '设计花束', '', 'u','s',None,[]))
    assert ui == UIType.PLAN_CARD
    assert data['task_id'] == 'real-task'
    assert data['plans'][0]['plan_id'] == 'DIY_valid'
    generate.assert_awaited_once()


def test_failed_submission_is_visible(monkeypatch):
    for method in ('get_session_flag', 'set_session_flag', 'clear_session_flags'):
        monkeypatch.setattr(module.mem_store, method, AsyncMock(return_value=None))
    monkeypatch.setattr(module, '_clarify_slots', lambda *a, **k: [])
    monkeypatch.setattr(tools, 'generate_effect_image', AsyncMock(return_value='{"error":"unavailable"}'))
    _, ui, data, reply, _ = asyncio.run(ReActAgent()._post_process(
        None, [record('generate_diy_plan', {'plan_id':'DIY_valid','diy':True})],
        SessionStage.DIY_DESIGN, '设计花束', '方案完成', 'u','s',None,[]))
    assert ui == UIType.PLAN_CARD
    assert not data.get('task_id')
    assert '未提交成功' in reply


def test_material_negation_does_not_disable_preview():
    text = '给妈妈生日设计一束DIY花束，预算200元，只用康乃馨和洋桔梗，不要配花和叶材，粉白配色、纸包装，请给完整方案并生成效果图。'
    assert module._decline_image(text) is False
    assert module._decline_image('不要配花，效果图也不要') is True
    assert module._decline_image('不需要生成效果图') is True
    assert module._decline_image('不要改花材，生成效果图') is False
