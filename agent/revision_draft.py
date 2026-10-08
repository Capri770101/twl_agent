"""修改意向草稿与现有方案分开保存；确认只执行用户见过的摘要。"""
import json
import time
from backend.storage import memory
from agent.toolkit import register_tool

KEY = 'pending_revision_draft'
CONFIRM = '确认修改并生成新版'
CANCEL = '取消本次修改'


async def handle_confirmation(uid, sid, message):
    from agent.engine.ui_protocol import ChatResponse, UIType
    if message.strip().rstrip('。！!') not in (CONFIRM, CANCEL):
        return None
    draft = await memory.get_session_json(uid, sid, KEY)
    if message.strip().rstrip('。！!') == CANCEL:
        await memory.set_session_json(uid, sid, KEY, None)
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply='已取消本次修改，原方案保持不变。')
    current = await memory.get_session_json(uid, sid, 'latest_diy_plan')
    if not isinstance(draft, dict) or time.time() - draft.get('created_at', 0) > 1800 or not current or current.get('plan_id') != draft.get('base_plan_id'):
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply='修改草稿已过期或当前方案已切换，请重新确认修改内容。')
    from agent.diy_tools import revise_diy_plan
    raw = await revise_diy_plan(json.dumps(current), draft['summary'], {'user_id':uid,'session_id':sid,'revision_confirmed':True})
    plan = json.loads(raw)
    if plan.get('error'):
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply=plan['error'])
    await memory.set_session_json(uid, sid, KEY, None)
    from agent.restore_plan import restore_response
    from agent.agent import _FLAG_IMAGE_OPTOUT
    result = await restore_response(uid, sid, plan['version'], image_optout_key=_FLAG_IMAGE_OPTOUT)
    preview = '预览图已就绪。' if result.data.get('result_url') else '预览图正在准备。' if result.data.get('task_id') else '当前没有可用预览图；若未关闭生图，可稍后请求生成。'
    result.reply = f'已按你确认的修改摘要生成第{plan["version"]}版，完整用料与参考报价见方案卡。' + preview
    return result


@register_tool(name='prepare_revision', description='讨论修改后整理待确认摘要，不生成方案、不出图。summary必须完整包含本轮及此前已达成的修改、保留项和待澄清冲突；更新时提交完整摘要，不能仅给增量。向用户展示后等待确认。',
               parameters={'type':'object','properties':{'summary':{'type':'string'}},'required':['summary'],'additionalProperties':False},
               inject_context=True, tags=['diy'])
async def prepare_revision(summary: str, _context=None):
    ctx = _context or {}
    uid, sid = ctx.get('user_id'), ctx.get('session_id')
    if not uid or not sid or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2000:
        return json.dumps({'error':'无法保存修改草稿'})
    plan = await memory.get_session_json(uid, sid, 'latest_diy_plan')
    if not isinstance(plan, dict) or not plan.get('plan_id'):
        return json.dumps({'error':'请先生成一份方案'})
    draft = {'base_plan_id':plan['plan_id'], 'summary':summary.strip(), 'created_at':time.time()}
    await memory.set_session_json(uid, sid, KEY, draft)
    return json.dumps({'pending_revision':True, 'summary':draft['summary'], 'base_plan_id':plan['plan_id']}, ensure_ascii=False)
