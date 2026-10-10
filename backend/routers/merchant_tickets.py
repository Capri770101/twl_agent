"""本人商家联系单查询：仅展示智能体本地登记状态，不代表商家已接收。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import current_sensitive_user
from backend.storage.db import transaction

router = APIRouter(prefix='/api/merchant-tickets', tags=['customer-service'])


def _identity(user_id: str | None) -> str:
    # 开发环境可能关闭普通鉴权；这些含个人诉求的接口仍必须有真实 JWT。
    if not user_id:
        raise HTTPException(status_code=401, detail='请先登录后查询本人事项')
    return user_id


def _ticket(row: dict, *, include_detail: bool = False) -> dict:
    item = {key: row[key] for key in ('id', 'order_id', 'shop_id', 'shop_name',
                                      'category', 'summary', 'status', 'created_at', 'updated_at')}
    item['id'] = str(item['id'])
    item['created_at'] = str(item['created_at'])
    item['updated_at'] = str(item['updated_at'])
    item['delivery_scope'] = 'local_only'
    item['merchant_delivery_verified'] = False
    item['notice'] = '此处仅展示本地登记事项；尚无商家投递和接收回执，请勿将登记状态视为商家已收到。'
    if include_detail:
        item['detail'] = row['detail']
        item['contact_phone'] = row['contact_phone']
    return item


@router.get('')
async def list_my_tickets(limit: int = 20, offset: int = 0,
                          user_id: str | None = Depends(current_sensitive_user)) -> dict:
    uid = _identity(user_id)
    limit = max(1, min(limit, 100))
    offset = max(0, min(offset, 10000))
    with transaction() as conn:
        rows = conn.execute(
            'SELECT id, order_id, shop_id, shop_name, category, summary, status, created_at, updated_at '
            'FROM merchant_tickets WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
            (uid, limit, offset)).fetchall()
    return {'ok': True, 'data': {'items': [_ticket(row) for row in rows],
                                 'limit': limit, 'offset': offset}}


@router.get('/{ticket_id}')
async def get_my_ticket(ticket_id: int, user_id: str | None = Depends(current_sensitive_user)) -> dict:
    uid = _identity(user_id)
    if ticket_id < 1:
        raise HTTPException(status_code=404, detail='事项不存在')
    with transaction() as conn:
        row = conn.execute(
            'SELECT id, order_id, shop_id, shop_name, category, summary, detail, contact_phone, '
            'status, created_at, updated_at FROM merchant_tickets WHERE id = ? AND user_id = ?',
            (ticket_id, uid)).fetchone()
    if not row:
        # 不区分不存在与他人事项，不泄露记录是否存在。
        raise HTTPException(status_code=404, detail='事项不存在')
    return {'ok': True, 'data': _ticket(row, include_detail=True)}
