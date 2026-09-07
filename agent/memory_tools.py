"""用户画像与长期记忆工具。"""

from __future__ import annotations

import datetime
from typing import Any

from agent.toolkit import register_tool

# 历史消息时间戳统一按北京时间展示（库里存的是 UTC，直接显示会让用户早 8 小时）
_CST = datetime.timezone(datetime.timedelta(hours=8))
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


@register_tool(name='search_history', description='跨会话检索当前用户的历史对话（按关键词匹配历史消息内容）。用户提到「上次」「之前」「那家店」「我之前买过/问过什么」时先用它回溯历史，再据此回答；返回匹配的历史消息（时间、发言方、内容摘要、所属会话）。查不到就如实说没找到，绝不编造历史。', parameters={'type': 'object', 'properties': {'query': {'type': 'string', 'description': '检索关键词，取用户原话里的核心词（店名 / 花材 / 人名）；留空则返回最近几条历史消息'}, 'limit': {'type': 'integer', 'description': '返回条数，默认 8，最多 20'}}, 'required': []}, inject_context=True, tags=['memory', 'history'])
async def search_history(query: str = '', limit: int = 8, _context: dict | None=None) -> dict:
    """跨会话检索历史消息（只读，绝不写入）。"""
    try:
        user_id = _get_user_id(_context)
        if not user_id:
            return {'ok': False, 'error': '缺少 user_id，无法检索历史'}
        session_id = (_context or {}).get('session_id', '')
        rows = await memory.search_user_history(user_id, query, limit=limit, exclude_session=session_id or None)
        items: list[dict[str, Any]] = []
        for r in rows:
            content = (r.get('content') or '').strip()
            # 截断单条内容：历史消息可能很长，全量回填会撑爆上下文
            if len(content) > 300:
                content = content[:300] + '…'
            created = r.get('created_at')
            # PostgreSQL 的 timestamp 读出来是 datetime 对象（不是字符串），
            # 不能直接切片；naive 值按 UTC 解释后转北京时间，str 场景也兼容。
            if hasattr(created, 'strftime'):
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.timezone.utc)
                time_str = created.astimezone(_CST).strftime('%Y-%m-%d %H:%M')
            else:
                time_str = str(created or '')[:16]
            items.append({
                'time': time_str,
                'role': '用户' if r.get('role') == 'user' else '助手',
                'content': content,
                'session': r.get('title') or r.get('session_id'),
            })
        return {'ok': True, 'count': len(items), 'data': items}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
