"""修改意向草稿与现有方案分开保存；确认只执行用户见过的摘要。"""
import json
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

# 口语肯定确认（2026-10-10 端到端踩坑）：确认门此前只认 UI 按钮文案
# CONFIRM/CANCEL 两个字面短语，用户说「是的」永远不匹配 → 回落普通 LLM 轮 →
# 模型又调 prepare_revision 再问一遍 → 「承诺出新版但永远不出」死循环。
# 这里扩成「显式短语 ∪ 口语肯定」，否定词一票否决（与 agent.is_affirmative
# 同一策略；不 import agent 是防循环依赖，词表单独维护，两边同步改）。
_REVISE_YES_WORDS = (
    CONFIRM, '确认修改', '确认生成', '生成新版', '生成新版本', '出新版', '出新方案',
    '按这个改', '按这样改', '就这样改', '改吧', '生成吧', '出吧', '可以', '好的',
    '好嘞', '没问题', '是的', '对', '对滴', '嗯', '嗯嗯', '行', 'OK', 'ok', 'Okay',
    '可以吧', '确认', '同意', '就这么办', '开始吧', '执行吧', '做吧', '安排',
)
_REVISE_NO_WORDS = (
    CANCEL, '取消', '取消修改', '先不改', '不用改', '不要改', '算了', '再等等',
    '等一下', '等等', '再想想', '先不', '不要', '不用', '不行', '不可以', '否',
    '再看看', '换一个', '换个', '再改改', '还有问题', '不对', '不是这个',
    '错了', '重说', '我没说', '没让',
)


def _resolve_confirm_intent(message: str) -> str | None:
    """把用户口语解析成 confirm / cancel / None（None=与修改确认无关）。

    规则：否定词**一票否决**（「不是/不要/再等等」优先于一切肯定词）；
    命中肯定词且当前存在草稿 → confirm。中性消息（改需求、问问题）返回
    None 交给正常 LLM 轮——确认门绝不能把「预算改成 500」吞成确认。

    注：cancel 判定也走这里返回；但「先读草稿」在 handle_confirmation 里
    先行（无草稿 → 无条件 None），避免 QA 等普通消息（恰含「不要」字样）
    触发确认门发 DB 查询或误回「已取消修改」。
    """
    t = (message or '').strip().rstrip('。！!？?，,')
    if not t or len(t) > 30:
        return None  # 超过 30 字的都不是简单确认/取消
    if any(w in t for w in _REVISE_NO_WORDS):
        return 'cancel'
    if any(w == t or w in t for w in _REVISE_YES_WORDS):
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
    raw = await revise_diy_plan(json.dumps(current), draft['summary'], {'user_id':uid,'session_id':sid,'revision_confirmed':True})
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
