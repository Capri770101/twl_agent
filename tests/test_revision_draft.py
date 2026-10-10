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


import time

def test_cancel_does_not_revise(monkeypatch):
    # 草稿存在（非空 dict）且消息是取消短语 → 取消并清草稿，不执行修改
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(
        side_effect=[{'base_plan_id':'p1','created_at':time.time(),'summary':'x'}, {'plan_id':'p1'}]))
    monkeypatch.setattr(draft.memory, 'set_session_json', AsyncMock())
    revise = AsyncMock()
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    result = asyncio.run(draft.handle_confirmation('u','s',draft.CANCEL))
    assert '保持不变' in result.reply
    revise.assert_not_called()


def test_cancel_with_empty_draft_is_normal_chat(monkeypatch):
    """无草稿时说「取消」是普通对话（别的问题的取消），确认门不得吞掉。"""
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(return_value={}))
    revise = AsyncMock()
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    result = asyncio.run(draft.handle_confirmation('u','s',draft.CANCEL))
    assert result is None
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


# ── 口语确认（2026-10-10 端到端死循环回归）─────────────────────────────
# 踩坑链：用户「是的」不匹配 CONFIRM 字面短语 → 回落 LLM 轮 → 模型再调
# prepare_revision 再问确认 → 「承诺出新版但永远不出」。扩口语词表 + 否定否决。

def _confirm_env(monkeypatch, summary='预算300元，主花改粉雪山'):
    import time
    import agent.restore_plan as restore
    from agent.engine.ui_protocol import ChatResponse, UIType
    _draft = {'base_plan_id':'p1','created_at':time.time(),'summary':summary}
    _plan = {'plan_id':'p1'}
    state = {'n': 0}
    async def _get(uid, sid, key):
        state['n'] += 1
        if state['n'] == 1:
            return _draft      # 第一次：读草稿
        return _plan           # 第二次：读当前方案
    def _reset_n():
        state['n'] = 0
    monkeypatch.setattr(draft.memory, 'get_session_json', _get)
    monkeypatch.setattr(draft.memory, 'set_session_json', AsyncMock())
    revise = AsyncMock(return_value=json.dumps({'plan_id':'p2','version':2}))
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    monkeypatch.setattr(restore, 'restore_response', AsyncMock(return_value=ChatResponse(
        user_id='u', session_id='s', ui=UIType.PLAN_CARD, data={'plans':[{'plan_id':'p2'}]})))
    return revise, _reset_n


def test_colloquial_yes_confirms(monkeypatch):
    """「是的/好的/嗯/OK/生成吧」等口语肯定必须触发执行，不再死循环。"""
    revise, reset = _confirm_env(monkeypatch)
    for yes in ('是的', '好的', '嗯', '嗯嗯', 'OK', '生成吧', '就这样改吧', '对，出吧', '好嘞'):
        reset()
        revise.reset_mock()
        result = asyncio.run(draft.handle_confirmation('u','s',yes))
        assert result is not None, f'口语 {yes!r} 未触发确认执行'
        assert '已按你确认的修改摘要生成第2版' in result.reply, f'{yes!r}'
        revise.assert_awaited_once()


def test_negative_vetoes_even_with_yes_word(monkeypatch):
    """否定一票否决：「先不要改」「再等等」绝不能触发执行。"""
    revise, reset = _confirm_env(monkeypatch)
    for no in ('先不要改', '再等等', '算了', '不对', '不是这个', '等等，预算改成500行不行'):
        reset()
        revise.reset_mock()
        result = asyncio.run(draft.handle_confirmation('u','s',no))
        if result is not None:
            assert '取消' in result.reply or '保持不变' in result.reply, f'{no!r}'
        revise.assert_not_awaited()


def test_long_message_is_not_confirmation(monkeypatch):
    """改需求的长消息（「预算改成500，花材换成红玫瑰」）不是确认——正常走 LLM。"""
    revise, reset = _confirm_env(monkeypatch)
    result = asyncio.run(draft.handle_confirmation('u','s','预算改成500，花材换成红玫瑰'))
    assert result is None
    revise.assert_not_awaited()


def test_concurrent_confirmation_conflict_is_reported(monkeypatch):
    revise, _ = _confirm_env(monkeypatch)
    revise.side_effect = ValueError('方案已被其他请求更新')
    result = asyncio.run(draft.handle_confirmation('u', 's', '是的'))
    assert '没有再次生成' in result.reply
    assert '已按你确认' not in result.reply


def test_yes_without_draft_is_normal_chat(monkeypatch):
    """没有草稿时「是的」是普通对话（如在回答别的问题），确认门不得吞掉。"""
    monkeypatch.setattr(draft.memory, 'get_session_json', AsyncMock(side_effect=[None]))
    revise = AsyncMock()
    monkeypatch.setattr(diy_tools, 'revise_diy_plan', revise)
    result = asyncio.run(draft.handle_confirmation('u','s','是的'))
    assert result is None
    revise.assert_not_awaited()


def test_questions_and_new_constraints_do_not_execute_stale_draft(monkeypatch):
    revise, reset = _confirm_env(monkeypatch)
    for message in ('可以把预算改成500吗', '对比一下两版差别', '可以先说说价格吗',
                    '是的，预算改为500', '这个可以吗', '不要取消，按这个改',
                    '等等，预算改成500行不行'):
        reset()
        result = asyncio.run(draft.handle_confirmation('u', 's', message))
        assert result is None, message
    revise.assert_not_awaited()


def test_short_yes_executes_but_other_topic_is_not_confirmation(monkeypatch):
    revise, reset = _confirm_env(monkeypatch)
    assert asyncio.run(draft.handle_confirmation('u', 's', '好。')) is not None
    revise.assert_awaited_once()
    reset()
    assert asyncio.run(draft.handle_confirmation('u', 's', '不要方案和图片')) is None
