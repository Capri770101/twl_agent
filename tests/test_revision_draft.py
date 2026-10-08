import asyncio
import json
from unittest.mock import AsyncMock
from agent import revision_draft as draft
from agent import diy_tools


def test_prepare_does_not_change_active_plan(monkeypatch):
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(return_value={'plan_id':'p1'}))
    save = AsyncMock()
    monkeypatch.setattr(draft.memory, 'set_session_json', save)
    result = json.loads(asyncio.run(draft.prepare_revision('配色白色，保留支数', {'user_id':'u','session_id':'s'})))
    assert result['pending_revision']
    assert save.call_args.args[2] == draft.KEY
    assert save.call_args.args[3]['base_plan_id'] == 'p1'


def test_cancel_does_not_revise(monkeypatch):
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(return_value={}))
    monkeypatch.setattr(draft.memory, 'set_session_json', AsyncMock())
    revise = AsyncMock()
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    result = asyncio.run(draft.handle_confirmation('u','s',draft.CANCEL))
    assert '保持不变' in result.reply
    revise.assert_not_called()


def test_stale_base_refuses_confirmation(monkeypatch):
    import time
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(side_effect=[{'base_plan_id':'p1','created_at':time.time()}, {'plan_id':'p2'}]))
    result = asyncio.run(draft.handle_confirmation('u','s',draft.CONFIRM))
    assert '已切换' in result.reply


def test_confirm_executes_saved_summary_and_clears_draft(monkeypatch):
    import time
    import agent.restore_plan as restore
    from agent.engine.ui_protocol import ChatResponse, UIType
    summary = '配色改为白色，包装改为粉色，保留原花材和支数'
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(side_effect=[
        {'base_plan_id':'p1','created_at':time.time(),'summary':summary}, {'plan_id':'p1'}]))
    save = AsyncMock()
    monkeypatch.setattr(draft.memory, 'set_session_json', save)
    revise = AsyncMock(return_value=json.dumps({'plan_id':'p2','version':2}))
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    monkeypatch.setattr(restore, 'restore_response', AsyncMock(return_value=ChatResponse(
        user_id='u', session_id='s', ui=UIType.PLAN_CARD, data={'plans':[{'plan_id':'p2'}], 'task_id':'t2'})))
    result = asyncio.run(draft.handle_confirmation('u','s',draft.CONFIRM))
    assert revise.call_args.args[1] == summary
    assert revise.call_args.args[2]['revision_confirmed'] is True
    save.assert_awaited_once_with('u','s',draft.KEY,None)
    assert result.data['plans'][0]['plan_id'] == 'p2'
