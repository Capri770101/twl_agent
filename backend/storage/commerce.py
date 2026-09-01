"""独立封装版 commerce.py —— 最小下单能力。

契约（与原项目对齐）：
- items: [{plan_id, qty?, item_id?}]，price 由本模块从 plans 表查，不信任客户端。
- 返回: {order_id, total_price, discount, items, status}
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from backend.storage.db import get_conn, transaction


def _now() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _plan_price(pid: str) -> float:
    """从 plans 表取方案价格（DIY 方案回退 budget_breakdown.total_estimate）。"""
    if not pid:
        return 0.0
    conn = get_conn()
    row = conn.execute('SELECT price FROM plans WHERE id = ?', (pid,)).fetchone()
    if row and isinstance(row['price'], (int, float)) and row['price'] > 0:
        return float(row['price'])
    return 0.0


async def create_order(user_id: str, items: list[dict], **kwargs) -> dict[str, Any]:
    """创建订单：从 plans 表取价格，写 orders + order_items。"""
    order_id = f'ORD_{uuid.uuid4().hex[:12]}'
    now = _now()
    priced: list[dict] = []
    for it in items:
        pid = it.get('plan_id', '')
        price = _plan_price(pid)
        qty = int(it.get('qty', 1)) if it.get('qty') else 1
        priced.append({'plan_id': pid, 'price': price, 'qty': qty, 'name': it.get('name', '')})
    total = sum(float(i['price']) * i['qty'] for i in priced)
    with transaction() as conn:
        conn.execute(
            'INSERT INTO orders(id, user_id, shop_id, total, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
            (order_id, user_id, kwargs.get('shop_id', ''), total, 'created', now, now),
        )
        for p in priced:
            conn.execute(
                'INSERT INTO order_items(order_id, plan_id, name, price, quantity) VALUES (?,?,?,?,?)',
                (order_id, p['plan_id'], p['name'], p['price'], p['qty']),
            )
    return {'order_id': order_id, 'total_price': total, 'discount': 0, 'items': priced, 'status': 'created'}


async def get_order(order_id: str) -> dict[str, Any] | None:
    with transaction() as conn:
        row = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    return dict(row) if row else None
