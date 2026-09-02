"""简化版 memory.py —— 短期记忆 + 长期记忆，用于独立部署。"""
from __future__ import annotations
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from backend.storage.db import get_conn, transaction
from domain.requirements import FlowerRequirement

logger = logging.getLogger('memory')


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds')


async def get_or_create_session(user_id: str, conversation_id: str | None = None, shop_id: str | None = None) -> str:
    with transaction() as conn:
        if conversation_id:
            row = conn.execute('SELECT session_id FROM sessions WHERE session_id = ? AND user_id = ?', (conversation_id, user_id)).fetchone()
            if row:
                return row['session_id']
            conn.execute('INSERT INTO sessions(session_id, user_id, stage, title, shop_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
                         (conversation_id, user_id, 'analyze', '新对话', shop_id, _now(), _now()))
            return conversation_id
        session_id = uuid.uuid4().hex
        conn.execute('INSERT INTO sessions(session_id, user_id, stage, title, shop_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
                     (session_id, user_id, 'analyze', '新对话', shop_id, _now(), _now()))
        return session_id


async def create_conversation(user_id: str, title: str = '新对话', shop_id: str | None = None) -> str:
    """创建新会话，供 chat 路由直接调用。"""
    session_id = uuid.uuid4().hex
    with transaction() as conn:
        conn.execute(
            'INSERT INTO sessions(session_id, user_id, stage, title, shop_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
            (session_id, user_id, 'analyze', title[:50] or '新对话', shop_id, _now(), _now())
        )
    return session_id


async def save_messages(session_id: str, messages: list[dict[str, Any]]) -> None:
    """批量保存消息。"""
    for msg in messages:
        await save_message(
            session_id,
            str(msg.get('role', 'user')),
            str(msg.get('content', '') or ''),
            ui=msg.get('ui'),
            data=msg.get('data')
        )


async def get_stage(session_id: str) -> str:
    with transaction() as conn:
        row = conn.execute('SELECT stage FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
    return row['stage'] if row else 'analyze'


async def get_session_shop_id(session_id: str) -> str | None:
    with transaction() as conn:
        row = conn.execute('SELECT shop_id FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
    return row['shop_id'] if row else None


async def update_stage(session_id: str, stage: str) -> None:
    with transaction() as conn:
        conn.execute('UPDATE sessions SET stage = ?, updated_at = ? WHERE session_id = ?', (stage, _now(), session_id))


async def set_requirement(session_id: str, req: FlowerRequirement) -> None:
    """保存结构化需求。"""
    with transaction() as conn:
        conn.execute(
            'UPDATE sessions SET preview = ?, updated_at = ? WHERE session_id = ?',
            (json.dumps(req.to_dict(), ensure_ascii=False), _now(), session_id)
        )


async def get_requirement(session_id: str):
    with transaction() as conn:
        row = conn.execute('SELECT preview FROM sessions WHERE session_id = ?', (session_id,)).fetchone()
    if not row or not row['preview']:
        return None
    try:
        return FlowerRequirement.from_dict(json.loads(row['preview']))
    except Exception:
        return None


async def get_long_term(user_id: str) -> dict[str, Any]:
    with transaction() as conn:
        rows = conn.execute('SELECT key, value FROM user_preferences WHERE user_id = ?', (user_id,)).fetchall()
    result = {}
    for row in rows:
        try:
            result[row['key']] = json.loads(row['value'])
        except Exception:
            result[row['key']] = row['value']
    return result


async def set_long_term(user_id: str, key: str, value: Any) -> None:
    with transaction() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO user_preferences(user_id, key, value, updated_at) VALUES (?,?,?,?)',
            (user_id, key, json.dumps(value, ensure_ascii=False), _now())
        )


async def get_session_json(user_id: str, session_id: str, key: str):
    with transaction() as conn:
        row = conn.execute('SELECT value FROM memories WHERE user_id = ? AND category = ? AND key = ? LIMIT 1', (user_id, session_id, key)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row['value'])
    except Exception:
        return row['value']


async def set_session_json(user_id: str, session_id: str, key: str, value: Any) -> None:
    await upsert_user_memory(user_id, session_id, key, json.dumps(value, ensure_ascii=False))


async def set_session_flag(user_id: str, session_id: str, key: str, value: str) -> None:
    await upsert_user_memory(user_id, f'flag:{session_id}', key, value)


async def get_session_flag(user_id: str, session_id: str, key: str) -> str | None:
    with transaction() as conn:
        row = conn.execute('SELECT value FROM memories WHERE user_id = ? AND category = ? AND key = ? LIMIT 1', (user_id, f'flag:{session_id}', key)).fetchone()
    return row['value'] if row else None


async def clear_session_flags(user_id: str, session_id: str, prefix: str = '') -> None:
    with transaction() as conn:
        conn.execute('DELETE FROM memories WHERE user_id = ? AND category = ? AND key LIKE ?', (user_id, f'flag:{session_id}', f'{prefix}%'))


async def load_display_messages(session_id: str) -> list[dict[str, Any]]:
    return await load_history(session_id, 200)


async def load_history(conversation_id: str, limit: int) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute('SELECT role, content, ui, data FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?', (conversation_id, limit)).fetchall()

    messages: list[dict[str, Any]] = []
    for row in rows:
        role = row['role']
        if role == 'tool':
            continue
        msg: dict[str, Any] = {'role': role, 'content': row['content'] or ''}
        if role == 'assistant':
            ui = row['ui']
            if ui:
                try:
                    msg['ui'] = json.loads(ui) if isinstance(ui, str) else ui
                except (json.JSONDecodeError, TypeError):
                    pass
            data = row['data']
            if data:
                try:
                    msg['data'] = json.loads(data) if isinstance(data, str) else data
                except (json.JSONDecodeError, TypeError):
                    pass
        messages.append(msg)
    return messages


async def save_message(conversation_id: str, role: str, content: str, ui: Any = None, data: Any = None) -> None:
    with transaction() as conn:
        conn.execute('INSERT INTO messages(session_id, role, content, ui, data, created_at) VALUES (?,?,?,?,?,?)',
                     (conversation_id, role, content,
                      json.dumps(ui, ensure_ascii=False) if ui else None,
                      json.dumps(data, ensure_ascii=False) if data else None,
                      _now()))


async def update_conversation_preview(conversation_id: str, preview: str) -> None:
    with transaction() as conn:
        conn.execute('UPDATE sessions SET preview = ?, updated_at = ? WHERE session_id = ?', (preview[:200], _now(), conversation_id))


async def list_conversations(user_id: str) -> list[dict[str, Any]]:
    with transaction() as conn:
        rows = conn.execute('SELECT session_id, title, preview, shop_id, created_at, updated_at FROM sessions WHERE user_id = ? ORDER BY updated_at DESC', (user_id,)).fetchall()
    return [{'id': r['session_id'], 'title': r['title'] or '新对话', 'preview': r['preview'] or '', 'shop_id': r['shop_id'], 'created_at': r['created_at'], 'updated_at': r['updated_at']} for r in rows]


async def get_conversation(conversation_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute('SELECT session_id, user_id, title, preview, shop_id, created_at, updated_at FROM sessions WHERE session_id = ?', (conversation_id,)).fetchone()
    return dict(row) if row else None


async def delete_conversation(conversation_id: str) -> bool:
    with transaction() as conn:
        conn.execute('DELETE FROM messages WHERE session_id = ?', (conversation_id,))
        conn.execute('DELETE FROM sessions WHERE session_id = ?', (conversation_id,))
    return True


async def get_user_memories(user_id: str, category: str | None = None) -> list[dict[str, Any]]:
    with transaction() as conn:
        if category:
            rows = conn.execute('SELECT key, value, confidence FROM memories WHERE user_id = ? AND category = ?', (user_id, category)).fetchall()
        else:
            rows = conn.execute('SELECT key, value, confidence FROM memories WHERE user_id = ?', (user_id,)).fetchall()
    return [dict(r) for r in rows]


async def upsert_user_memory(user_id: str, category: str, key: str, value: str, confidence: float = 1.0) -> None:
    with transaction() as conn:
        conn.execute('INSERT OR REPLACE INTO memories(user_id, category, key, value, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
                     (user_id, category, key, value, confidence, _now(), _now()))
