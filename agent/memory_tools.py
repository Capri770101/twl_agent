"""用户画像与长期记忆工具。"""

from __future__ import annotations

from agent.toolkit import register_tool
from backend.storage import memory
from domain.requirements import FlowerRequirement


def _get_user_id(_context: dict | None) -> str:
    return (_context or {}).get('user_id', '')


async def save_user_profile_from_requirement(user_id: str, req: FlowerRequirement | None) -> dict[str, str]:
    """从结构化需求中提取用户偏好并写入长期记忆（本地用户画像）。"""
    if not user_id or not req:
        return {}
    profile: dict[str, str] = {}
    if req.recipient:
        profile['preferred_recipient'] = req.recipient
    if req.occasion:
        profile['preferred_occasion'] = req.occasion
    if req.style:
        profile['preferred_style'] = req.style
    if req.mood:
        profile['preferred_mood'] = req.mood
    if req.colors:
        profile['preferred_colors'] = ','.join(req.colors)
    if req.budget_num is not None:
        profile['budget_level'] = str(int(req.budget_num))
    for k, v in profile.items():
        await memory.set_long_term(user_id, k, v)
    return profile


@register_tool(name='get_user_profile', description='获取当前用户的长期偏好画像（送花对象/场合/风格/色系/预算等）。', parameters={'type': 'object', 'properties': {}, 'required': []}, inject_context=True, tags=['user', 'memory'])
async def get_user_profile(_context: dict | None=None) -> str:
    try:
        user_id = _get_user_id(_context)
        profile = await memory.get_long_term(user_id)
        return {'ok': True, 'data': profile}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


@register_tool(name='save_user_profile', description='手动保存一条用户长期偏好，例如 key=preferred_style value=韩式。', parameters={'type': 'object', 'properties': {'key': {'type': 'string', 'description': '偏好键，如 preferred_style / preferred_colors / budget_level'}, 'value': {'type': 'string', 'description': '偏好值'}}, 'required': ['key', 'value']}, inject_context=True, tags=['user', 'memory'])
async def save_user_profile(key: str, value: str, _context: dict | None=None) -> str:
    try:
        user_id = _get_user_id(_context)
        await memory.set_long_term(user_id, key, value)
        return {'ok': True, 'saved': {key: value}}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


@register_tool(name='save_memory', description='把用户明确表达的偏好写入长期记忆（如预算、送花对象、偏好色系）。', parameters={'type': 'object', 'properties': {'key': {'type': 'string', 'description': '偏好键，如 budget / recipient / color'}, 'value': {'type': 'string', 'description': '偏好值'}}, 'required': ['key', 'value']}, inject_context=True, tags=['memory'])
async def save_memory(key: str, value: str, _context: dict | None=None) -> str:
    """写入用户长期偏好。"""
    user_id = _get_user_id(_context) or 'anonymous'
    await memory.set_long_term(user_id, key, value)
    return {'saved': {key: value}}
