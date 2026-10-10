"""修改意向草稿与现有方案分开保存；确认只执行用户见过的摘要。"""
import json
import re
import time
from backend.storage import memory
from agent.toolkit import register_tool


def _store(store=None):
    """存储解析：优先用调用方传入的 store（agent.run 传入 mem_store，便于
    测试注入 FakeMemory），缺省回落模块级 memory——两种用法都合法。"""
    return store if store is not None else memory


KEY = 'pending_revision_draft'
CONFIRM = '确认修改并生成新版'
CANCEL = '取消本次修改'

# 对改版这种会改变现有方案的操作，只接受独立、明确的确认/取消短句。
# 含新条件、提问或对比的消息一律交回正常对话处理，不能靠「对」「可以」子串猜。
_REVISE_YES_WORDS = frozenset((
    CONFIRM, '确认修改', '确认生成', '生成新版', '生成新版本', '出新版', '出新方案',
    '按这个改', '按这样改', '就这样改', '改吧', '生成吧', '出吧', '可以', '好的',
    '好', '好嘞', '没问题', '是的', '对', '对滴', '嗯', '嗯嗯', '行', 'ok',
    '确认', '同意', '就这么办', '就这样改吧', '开始吧', '执行吧', '做吧', '安排',
    '对出吧', '好的出吧', '是的出吧', '可以生成',
))
_REVISE_NO_WORDS = frozenset((
    CANCEL, '取消修改', '先不改', '不用改', '不要改', '算了', '再等等',
    '等一下', '等等', '再想想', '先不', '不行', '不可以', '再看看',
))


def _resolve_confirm_intent(message: str) -> str | None:
    """只对完整确认/取消短句执行草稿；复杂表达交给正常对话澄清。"""
    t = re.sub(r'[\s。！!？?，,、]+', '', str(message or '').strip()).lower()
    if t in _REVISE_NO_WORDS:
        return 'cancel'
    if t in _REVISE_YES_WORDS:
        return 'confirm'
    return None


async def handle_confirmation(uid, sid, message, store=None):
    from agent.engine.ui_protocol import ChatResponse, UIType
    st = _store(store)
    draft = await st.get_session_json(uid, sid, KEY)
    if not isinstance(draft, dict) or not draft:
        # 无草稿（含 FakeMemory 宽松返回 {} 的情况）：一切确认/取消语都与
        # 修改无关（如对别的问题答「是的」、QA 消息里恰好含「不要」字样）
        # ——交回普通 LLM 轮。
        return None
    intent = _resolve_confirm_intent(message)
    if intent is None:
        return None
    if intent == 'cancel':
        await st.set_session_json(uid, sid, KEY, None)
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply='已取消本次修改，原方案保持不变。')
    current = await st.get_session_json(uid, sid, 'latest_diy_plan')
    if time.time() - draft.get('created_at', 0) > 1800 or not current or current.get('plan_id') != draft.get('base_plan_id'):
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply='修改草稿已过期或当前方案已切换，请重新确认修改内容。')
    from agent.diy_tools import revise_diy_plan
    try:
        raw = await revise_diy_plan(json.dumps(current), draft['summary'], {'user_id':uid,'session_id':sid,'revision_confirmed':True})
    except ValueError:
        # 版本入库按 sessions 行锁串行化；第二个同时确认的请求会因父版本
        # 已切换被拒，不能让用户看到 500 或声称也生成了另一版。
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design',
                            reply='当前方案已被另一个请求更新，本次没有再次生成。请查看最新方案后重新提出修改。')
    plan = json.loads(raw)
    if plan.get('error'):
        return ChatResponse(user_id=uid, session_id=sid, stage='diy_design', reply=plan['error'])
    await st.set_session_json(uid, sid, KEY, None)
    from agent.restore_plan import restore_response
    from agent.agent import _FLAG_IMAGE_OPTOUT
    result = await restore_response(uid, sid, plan['version'], image_optout_key=_FLAG_IMAGE_OPTOUT)
    preview = '预览图已就绪。' if result.data.get('result_url') else '预览图正在准备。' if result.data.get('task_id') else '当前没有可用预览图；若未关闭生图，可稍后请求生成。'
    result.reply = f'已按你确认的修改摘要生成第{plan["version"]}版，完整用料与参考报价见方案卡。' + preview
    return result


@register_tool(name='prepare_revision', description='讨论修改后整理待确认摘要，不生成方案、不出图。summary必须完整包含本轮及此前已达成的修改、保留项和待澄清冲突；更新时提交完整摘要，不能仅给增量。向用户展示摘要并明确问「是否按此生成新版？」，等用户答复确认（按钮或口语「是的/可以/生成吧」都可以）。',
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
    return json.dumps({'pending_revision':True, 'summary':draft['summary'], 'base_plan_id':plan['plan_id'],
                       'note':'摘要已存为待确认草稿；用户下一条消息若是肯定答复，系统会直接执行生成新版，无需再次调用本工具'}, ensure_ascii=False)
