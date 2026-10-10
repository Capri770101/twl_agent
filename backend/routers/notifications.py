"""提醒/通知站内 API：本人收件箱、提醒规则管理。"""
from __future__ import annotations

import asyncio
import datetime as _dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import current_user
from backend.reminders import _FIRST_RUN_RE, _SHANGHAI, _UTC, _next_after, parse_cron
from backend.storage.db import transaction

router = APIRouter(prefix='/api', tags=['notifications'])


def _row(r) -> dict:
    return dict(r) if hasattr(r, 'keys') else dict(r)


@router.get('/notifications')
async def list_notifications(user_id: str = Depends(current_user),
                             limit: int = 50, offset: int = 0) -> dict:
    return await asyncio.to_thread(_list_notifications_sync, user_id, limit, offset)


def _list_notifications_sync(user_id: str, limit: int, offset: int) -> dict:
    """收件箱：到点提醒（type=reminder）等通知，按时间倒序。

    接入方轮询或长轮询该端点即可把提醒透出给用户；is_read=0 为未读。
    分页只防滥用：limit 1..100、offset 0..10000。
    """
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, min(int(offset or 0), 10000))
    with transaction() as conn:
        rows = conn.execute(
            'SELECT id, type, title, body, is_read, created_at FROM notifications '
            'WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?',
            (user_id, limit, offset)).fetchall()
        unread = conn.execute(
            'SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0',
            (user_id,)).fetchone()
    items = [{
        'id': str(_row(r)['id']),
        'type': _row(r)['type'],
        'title': _row(r)['title'],
        'body': _row(r)['body'],
        'is_read': bool(_row(r)['is_read']),
        'created_at': str(_row(r)['created_at']),
    } for r in rows]
    return {'ok': True, 'data': {'items': items, 'unread': int(_row(unread)['n'] or 0)}}


@router.post('/notifications/{nid}/read')
async def mark_read(nid: str, user_id: str = Depends(current_user)) -> dict:
    if not nid or len(nid) > 80:
        return {'ok': False, 'code': 'INVALID_ARGUMENT'}
    return await asyncio.to_thread(_mark_read_sync, nid, user_id)


def _mark_read_sync(nid: str, user_id: str) -> dict:
    """标记已读（属主 WHERE 内建：别人的通知改不动）。"""
    with transaction() as conn:
        cur = conn.execute('UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?',
                           (nid, user_id))
        changed = cur.rowcount or 0
    return {'ok': True, 'data': {'updated': changed}}


@router.get('/reminders')
async def list_reminders(user_id: str = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_list_reminders_sync, user_id)


def _list_reminders_sync(user_id: str) -> dict:
    """本人提醒规则（含停用项；status=active 的才会触发）。"""
    with transaction() as conn:
        rows = conn.execute(
            'SELECT ' + _REMINDER_COLUMNS + ' FROM reminders '
            'WHERE user_id = ? ORDER BY id DESC LIMIT 100',
            (user_id,)).fetchall()
    return {'ok': True, 'data': {'items': [_reminder_data(r) for r in rows]}}


def _reminder_data(row) -> dict:
    r = _row(row)
    next_run = r['next_run']
    # PostgreSQL TIMESTAMPTZ is an aware instant, regardless of server timezone.
    return {'id': r['id'], 'content': r['content'], 'cron': r['cron'],
            'first_run': r['first_run'], 'repeat': bool(r['repeat']),
            'next_run': next_run.isoformat(),
            'next_run_local': next_run.astimezone(_SHANGHAI).strftime('%Y-%m-%d %H:%M'),
            'timezone': 'Asia/Shanghai', 'status': r['status']}


_REMINDER_COLUMNS = 'id, content, cron, first_run, repeat, next_run, status'


class ReminderPatch(BaseModel):
    content: str | None = None
    cron: str | None = None
    first_run: str | None = None


def _invalid(reason: str) -> dict:
    return {'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': reason}


def _not_found() -> dict:
    return {'ok': False, 'code': 'NOT_FOUND', 'reason': '提醒不存在或不属于你'}


def _schedule(cron: str | None, first_run: str | None, now: _dt.datetime):
    """Compute an instant from the same Shanghai rules used on creation."""
    if cron is not None:
        parsed = parse_cron(cron)
        if parsed is None:
            return None
        nxt = _next_after(parsed, now)
        return (cron, None, True, nxt) if nxt is not None else None
    if not isinstance(first_run, str) or not _FIRST_RUN_RE.fullmatch(first_run):
        return None
    try:
        nxt = _dt.datetime.strptime(first_run, '%Y-%m-%d %H:%M').replace(
            tzinfo=_SHANGHAI).astimezone(_UTC)
    except ValueError:
        return None
    return (None, first_run, False, nxt) if nxt > now else None


@router.post('/reminders/{rid}/pause')
async def pause_reminder(rid: int, user_id: str = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_pause_reminder_sync, rid, user_id)


def _pause_reminder_sync(rid: int, user_id: str) -> dict:
    """Pause only an active owned rule; scheduler and this UPDATE lock the same row."""
    with transaction() as conn:
        row = conn.execute(
            "UPDATE reminders SET status='paused', updated_at=NOW() "
            "WHERE id=? AND user_id=? AND status='active' RETURNING " + _REMINDER_COLUMNS,
            (rid, user_id)).fetchone()
    return {'ok': True, 'data': _reminder_data(row)} if row else _not_found()


@router.post('/reminders/{rid}/resume')
async def resume_reminder(rid: int, user_id: str = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_resume_reminder_sync, rid, user_id)


def _resume_reminder_sync(rid: int, user_id: str) -> dict:
    """Never replay missed occurrences: calculate a fresh future run on resume."""
    with transaction() as conn:
        row = conn.execute('SELECT ' + _REMINDER_COLUMNS +
                           ' FROM reminders WHERE id=? AND user_id=? AND status=? FOR UPDATE',
                           (rid, user_id, 'paused')).fetchone()
        if row is None:
            return _not_found()
        r = _row(row)
        now = _dt.datetime.now(_UTC)
        schedule = _schedule(r['cron'] if r['repeat'] else None,
                             r['first_run'] if not r['repeat'] else None, now)
        if schedule is None:
            return _invalid('提醒时间已过或规则无效，请先修改日程')
        row = conn.execute(
            "UPDATE reminders SET next_run=?, status='active', updated_at=NOW() "
            'WHERE id=? AND user_id=? AND status=? RETURNING ' + _REMINDER_COLUMNS,
            (schedule[3], rid, user_id, 'paused')).fetchone()
    return {'ok': True, 'data': _reminder_data(row)}


@router.patch('/reminders/{rid}')
async def modify_reminder(rid: int, payload: ReminderPatch,
                          user_id: str = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_modify_reminder_sync, rid, payload, user_id)


def _modify_reminder_sync(rid: int, payload: ReminderPatch, user_id: str) -> dict:
    """Edit content and/or schedule of an active or paused owned reminder.

    Supply cron for a recurring schedule, or first_run for a one-off Shanghai
    wall time. A schedule edit recomputes next_run even while paused; it does
    not implicitly activate a paused reminder.
    """
    fields = payload.model_fields_set
    if not fields or any(getattr(payload, key) is None for key in fields):
        return _invalid('请提供非空的 content、cron 或 first_run')
    if 'cron' in fields and 'first_run' in fields:
        return _invalid('cron 与 first_run 只能选择一个')
    content = payload.content.strip() if 'content' in fields else None
    if 'content' in fields and (not content or len(content) > 200):
        return _invalid('content 长度须为 1 至 200 字符')
    with transaction() as conn:
        row = conn.execute('SELECT ' + _REMINDER_COLUMNS +
                           " FROM reminders WHERE id=? AND user_id=? AND status IN ('active','paused') FOR UPDATE",
                           (rid, user_id)).fetchone()
        if row is None:
            return _not_found()
        old = _row(row)
        schedule = None
        if 'cron' in fields or 'first_run' in fields:
            schedule = _schedule(payload.cron if 'cron' in fields else None,
                                 payload.first_run if 'first_run' in fields else None,
                                 _dt.datetime.now(_UTC))
            if schedule is None:
                return _invalid('cron 格式不合法、两年内无触发时间或 first_run 不在未来')
        cron, first_run, repeat, next_run = schedule if schedule else (
            old['cron'], old['first_run'], old['repeat'], old['next_run'])
        row = conn.execute(
            'UPDATE reminders SET content=?, cron=?, first_run=?, repeat=?, next_run=?, '
            'updated_at=NOW() WHERE id=? AND user_id=? RETURNING ' + _REMINDER_COLUMNS,
            (content if content is not None else old['content'], cron, first_run,
             repeat, next_run, rid, user_id)).fetchone()
    return {'ok': True, 'data': _reminder_data(row)}


@router.delete('/reminders/{rid}')
async def delete_reminder(rid: int, user_id: str = Depends(current_user)) -> dict:
    return await asyncio.to_thread(_delete_reminder_sync, rid, user_id)


def _delete_reminder_sync(rid: int, user_id: str) -> dict:
    """删除本人提醒规则（软删：status→cancelled，调度器不再扫它）。"""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled', updated_at = NOW() "
            'WHERE id = ? AND user_id = ? AND status != ?',
            (rid, user_id, 'cancelled'))
        changed = cur.rowcount or 0
    return {'ok': True, 'data': {'deleted': changed}} if changed else \
        {'ok': False, 'code': 'NOT_FOUND', 'reason': '提醒不存在或不属于你'}
