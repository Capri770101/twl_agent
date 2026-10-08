import asyncio
import copy
from unittest.mock import AsyncMock
import agent.agent as module
import agent.tools as tools
from agent.agent import ReActAgent


def test_switch_restores_exact_snapshot_and_emits_card_without_llm(monkeypatch):
    plan = {'plan_id':'DIY_first', 'version':1, 'diy':True, 'price':180, 'design':{'main_flowers':[{'name':'康乃馨','qty':9}]}}
    memory = module.mem_store
    for name, value in [('get_or_create_session','sid'), ('get_session_context',{}), ('get_stage','done'),
                        ('get_session_json',[plan]), ('get_session_flag',None)]:
        monkeypatch.setattr(memory, name, AsyncMock(return_value=value))
    monkeypatch.setattr(memory, 'store_plan_version', AsyncMock(return_value=copy.deepcopy(plan)))
    monkeypatch.setattr(memory, 'save_messages', AsyncMock())
    monkeypatch.setattr(memory, 'set_session_json', AsyncMock())
    monkeypatch.setattr(memory, 'update_stage', AsyncMock())
    monkeypatch.setattr(module, 'call_llm', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not call model')))
    image = AsyncMock(return_value='{"task_id":"old-image","result_url":"/generated/old-image.png","reused":true}')
    monkeypatch.setattr(tools, 'generate_effect_image', image)
    events = []
    result = asyncio.run(ReActAgent().run('u','用回第1版','sid',None,on_event=events.append))
    assert result.data['plans'][0] == plan
    assert result.data['poll'] == '/tasks/old-image'
    assert any(e['event'] == 'card' for e in events)
    image.assert_awaited_once_with('DIY_first', {'user_id':'u','session_id':'sid'})
    assert result.session_id == 'sid'
