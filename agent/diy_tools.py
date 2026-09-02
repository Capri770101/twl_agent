"""DIY 方案设计与改版工具。"""

from __future__ import annotations

import json
from typing import Any

from agent.toolkit import register_tool
from backend.storage import tasks


async def _store_diy_plan(plan: dict, _context: dict | None) -> None:
    """把最新 DIY 方案写入当前会话。"""
    uid = (_context or {}).get('user_id', '')
    sid = (_context or {}).get('session_id', '')
    if not uid or not sid:
        return
    # 延迟导入，避免循环依赖
    from backend.storage import memory as _memory
    await _memory.set_session_json(uid, sid, 'latest_diy_plan', plan)
    await _memory.set_session_json(uid, sid, 'selected_plan', plan)


def _parse_plan(plan: str) -> dict:
    if isinstance(plan, dict):
        return plan
    text = plan.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}


@register_tool(name='generate_diy_plan', description='根据用户需求设计一份结构化 DIY 花艺方案：抽取维度→查知识库→组装主花/配材/配比、色彩方案、包装、寓意文案与预算估算，并返回可供生图的 effect_prompt。输出另含分步插花指引(diy_steps)、养护建议(care_tips)、贺卡寄语文案(card_message)与预算明细(budget_breakdown)。', parameters={'type': 'object', 'properties': {'requirements': {'type': 'string', 'description': '用户的 DIY 需求描述'}}, 'required': ['requirements']}, inject_context=True, tags=['diy'])
async def generate_diy_plan(requirements: str, _context: dict | None=None) -> str:
    from agent.tools import design_diy_plan

    plan = design_diy_plan(requirements)
    await _store_diy_plan(plan, _context)
    return json.dumps(plan, ensure_ascii=False)


@register_tool(name='revise_diy_plan', description='基于已有方案 + 自然语言反馈，调整出下一版花艺方案：可调预算（便宜点/高档）、改风格、改色系、移除指定花材（不要X/去掉X）。返回带 version 与 parent_id 的可追溯新方案。', parameters={'type': 'object', 'properties': {'plan': {'type': 'string', 'description': '上一版方案 JSON 或含 JSON 的文本'}, 'feedback': {'type': 'string', 'description': '用户反馈，如 便宜点/换成红玫瑰/不要康乃馨/颜色再大胆'}}, 'required': ['plan', 'feedback']}, inject_context=True, tags=['diy'])
async def revise_diy_plan(plan: str, feedback: str, _context: dict | None=None) -> str:
    from agent.tools import revise_with_llm

    new_plan = revise_with_llm(plan, feedback)
    await _store_diy_plan(new_plan, _context)
    return json.dumps(new_plan, ensure_ascii=False)


@register_tool(name='generate_effect_image', description='为 DIY 方案提交 AI 生图任务（方案设计完成后系统会自动调用，无需用户确认）。若传入 latest_diy 则自动使用最近一次设计的方案生成精确 prompt（花材/色彩/形态/包装一致）；也可直接传入自定义描述。立即返回 task_id，客户端通过 GET /tasks/{task_id} 轮询。', parameters={'type': 'object', 'properties': {'plan': {'type': 'string', 'description': '方案描述或方案 ID；latest/latest_diy 表示使用最近设计的方案'}}, 'required': ['plan']}, tags=['image'], inject_context=True)
async def generate_effect_image(plan: str='latest_diy', _context: dict | None=None) -> str:
    from agent.tools import generate_effect_image as _generate_effect_image

    return await _generate_effect_image(plan=plan, _context=_context)
