"""提醒/通知读取 API：到点收件箱 + 提醒规则管理。

为什么补这个（2026-10-10 交付前查漏）：set_reminder 只负责**登记**
（写 reminders + 到点写 notifications），但接入方没有任何 HTTP 通道能
**读**到这些通知——提醒生成了用户看不见，等于哑炮。本路由补齐读取侧：

- GET /notifications  —— 收件箱（到点提醒 + 站内信统一出口）
- GET /reminders      —— 本人提醒规则列表
- DELETE /reminders/{id} —— 删除本人提醒规则

鉴权与 chat 一致：Bearer JWT（/auth/token 或 /auth/customer-token 换发），
user_id 从 token 解出，**不信任请求体**——属主校验内建在 WHERE 条件里，
不可能读到别人的提醒/通知。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from backend.auth import current_user
from backend.storage.db import transaction

router = APIRouter(prefix='/api', tags=['notifications'])


def _row(r) -> dict:
    return dict(r) if hasattr(r, 'keys') else dict(r)


@router.get('/notifications')
async def list_notifications(user_id: str = Depends(current_user),
                             limit: int = 50, offset: int = 0) -> dict:
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
    """标记已读（属主 WHERE 内建：别人的通知改不动）。"""
    if not nid or len(nid) > 80:
        return {'ok': False, 'code': 'INVALID_ARGUMENT'}
    with transaction() as conn:
        cur = conn.execute('UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?',
                           (nid, user_id))
        changed = cur.rowcount or 0
    return {'ok': True, 'data': {'updated': changed}}


@router.get('/reminders')
async def list_reminders(user_id: str = Depends(current_user)) -> dict:
    """本人提醒规则（含停用项；status=active 的才会触发）。"""
    with transaction() as conn:
        rows = conn.execute(
            'SELECT id, content, cron, first_run, repeat, next_run, status, created_at '
            "FROM reminders WHERE user_id = ? ORDER BY id DESC LIMIT 100",
            (user_id,)).fetchall()
    items = [{
        'id': _row(r)['id'],
        'content': _row(r)['content'],
        'cron': _row(r)['cron'],
        'first_run': _row(r)['first_run'],
        'repeat': bool(_row(r)['repeat']),
        'next_run': str(_row(r)['next_run']),
        'status': _row(r)['status'],
    } for r in rows]
    return {'ok': True, 'data': {'items': items}}


@router.delete('/reminders/{rid}')
async def delete_reminder(rid: int, user_id: str = Depends(current_user)) -> dict:
    """删除本人提醒规则（软删：status→cancelled，调度器不再扫它）。"""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled', updated_at = NOW() "
            'WHERE id = ? AND user_id = ? AND status != ?',
            (rid, user_id, 'cancelled'))
        changed = cur.rowcount or 0
    return {'ok': True, 'data': {'deleted': changed}} if changed else \
        {'ok': False, 'code': 'NOT_FOUND', 'reason': '提醒不存在或不属于你'}
