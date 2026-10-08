"""版本切换是确定性操作，不调用模型重新设计。"""
import copy
import json
import re
from backend.storage import memory
from agent.engine.ui_protocol import ChatResponse, UIType


def restore_version(message: str) -> int | None:
    match = re.fullmatch(r'\s*(?:请)?(?:用回|恢复到|恢复|回到|切回)(?:第)?([一二三四五六七八九十]|\d+)版[。！!\s]*', message)
    if not match:
        return None
    raw = match.group(1)
    return int(raw) if raw.isdigit() else '一二三四五六七八九十'.index(raw) + 1


async def restore_response(user_id: str, sid: str, version: int, *, image_optout_key: str) -> ChatResponse:
    history = await memory.get_session_json(user_id, sid, 'diy_plan_versions') or []
    target = next((p for p in history if isinstance(p, dict) and p.get('version') == version), None) if isinstance(history, list) else None
    if target is None:
        return ChatResponse(user_id=user_id, session_id=sid, stage='diy_design', reply=f'没有找到第{version}版的已保存快照，当前方案未改变。请从已有版本中选择。')
    plan = await memory.store_plan_version(user_id, sid, copy.deepcopy(target))
    from agent.revision_draft import KEY
    await memory.set_session_json(user_id, sid, KEY, None)
    data = {'plans': [plan]}
    reply = f'已完整切回第{version}版，花材、数量、配色、包装和参考报价均使用该版保存的数据，没有重新设计。'
    if await memory.get_session_flag(user_id, sid, image_optout_key) == '1':
        reply += '当前会话已关闭自动出图；如需预览，请说“给当前方案生成效果图”。'
    else:
        try:
            from agent.tools import generate_effect_image
            raw = await generate_effect_image(plan['plan_id'], {'user_id': user_id, 'session_id': sid})
            task = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(task, dict) or not task.get('task_id'):
                raise ValueError('missing task')
            data.update({k: task[k] for k in ('task_id', 'result_url') if task.get(k)})
            data['poll'] = '/tasks/' + task['task_id']
            reply += '预览图已恢复。' if task.get('result_url') else '预览图任务已就绪，完成后会显示。'
        except Exception:
            reply += '方案已恢复，但预览图暂未取得，请稍后重试生成效果图。'
    return ChatResponse(user_id=user_id, session_id=sid, ui=UIType.PLAN_CARD, data=data, reply=reply, stage='diy_design')
