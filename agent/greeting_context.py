"""从当前会话提取贺卡所需的稳定上下文，不把原始订单/用户隐私直接交给模型。"""
from __future__ import annotations

from typing import Any


def plan_context(plan: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(plan, dict):
        return {}
    design = plan.get('design') if isinstance(plan.get('design'), dict) else {}
    flowers: list[str] = []
    for key in ('main_flowers', 'fillers', 'foliage'):
        for item in design.get(key) or []:
            if isinstance(item, dict) and item.get('name'):
                qty = item.get('qty')
                flowers.append(f"{item['name']}×{qty}" if qty else str(item['name']))
    return {
        'plan_name': str(plan.get('name') or ''),
        'recipient': str(plan.get('recipient') or ''),
        'occasion': str(plan.get('occasion') or ''),
        'style': str(plan.get('style') or ''),
        'colors': '、'.join(str(x) for x in (design.get('color_scheme') or plan.get('color_scheme') or []) if x),
        'flowers': '、'.join(flowers),
        'meaning': str(design.get('meaning') or plan.get('meaning') or ''),
        'customer_message': str(plan.get('card_message') or ''),
    }


async def current_greeting_context(context: dict[str, Any] | None) -> dict[str, str]:
    ctx = context or {}
    uid, sid = str(ctx.get('user_id') or ''), str(ctx.get('session_id') or '')
    if not uid or not sid:
        return {}
    from backend.storage import memory
    plan = await memory.get_session_json(uid, sid, 'selected_plan')
    if not plan:
        plan = await memory.get_session_json(uid, sid, 'latest_diy_plan')
    return plan_context(plan)
