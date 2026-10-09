"""商家联系单：登记用户诉求并返回店铺联系方式。

设计边界（2026-10-09，与老板确认的职责划分）：
- 智能体职责是**信息桥**——把用户诉求结构化登记（工单落 agent 自己的 PG）、
  查出店铺电话给用户，让双方能立刻建立联系。
- 真正的商家通知通道（企业微信、商家后台 IM）属于 H5/商家系统，本模块
  **不实现**。他们接入时可以消费 merchant_tickets 表或提供工单 API。
- agent 只创建与查询工单，不改状态（open→forwarded/closed 由运维侧推进）。
"""
from __future__ import annotations

import re

from backend.config import settings
from backend.storage.db import transaction
from backend.customer_service import failure
from backend.customer_shops import query_shops

_ORDER_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')
_SHOP_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')
_PHONE_RE = re.compile(r'^[0-9+]{5,20}$')
_CATEGORIES = frozenset({'general', 'order', 'after_sales', 'custom', 'complaint'})
_SUMMARY_MAX = 200
_DETAIL_MAX = 2000


def _clean(text: str, limit: int) -> str:
    return re.sub(r'[\r\n\t]+', ' ', str(text or '')).strip()[:limit]


async def contact_merchant(user_id: str, *, order_id: str = '', shop_id: str = '', shop_name: str = '',
                           category: str = 'general', summary: str = '', detail: str = '',
                           contact_phone: str = '') -> dict:
    """登记一条商家联系单并返回店铺联系电话。

    Args:
        user_id: 请求上下文里的用户 id（未登录时是平台匿名 id，同样可登记）。
        order_id: 关联订单号（可选，售后类诉求一般都有）。
        shop_id/shop_name: 店铺编号或名称关键词（名称会走 shops 降级查询解析）。
        category: general/order/after_sales/custom/complaint。
        summary: 用户诉求一句话摘要（模型从对话里提炼，≤200 字）。
        detail: 完整诉求原文（≤2000 字）。
        contact_phone: 用户愿意留的回拨电话（可选）。
    Returns:
        ok=True 时 data 里带 ticket_id、店铺 service_phone（若解析到）与
        status='open'；解析不到店铺时 service_phone 为 None，不阻断工单创建。
    """
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    user_id = _clean(user_id, 80)
    if not user_id:
        return failure('AUTH_REQUIRED')
    summary = _clean(summary, _SUMMARY_MAX)
    if not summary:
        return failure('INVALID_ARGUMENT')
    detail = _clean(detail, _DETAIL_MAX)
    order_id = _clean(order_id, 80)
    if order_id and not _ORDER_ID_RE.fullmatch(order_id):
        return failure('INVALID_ARGUMENT')
    shop_id = _clean(shop_id, 80)
    if shop_id and not _SHOP_ID_RE.fullmatch(shop_id):
        # 模型常把店名直接传进 shop_id——按名称关键词降级解析（与 query_shop_service 同思路）。
        shop_name = shop_name or shop_id
        shop_id = ''
    shop_name = _clean(shop_name, 64)
    contact_phone = _clean(contact_phone, 20)
    if contact_phone and not _PHONE_RE.fullmatch(contact_phone):
        return failure('INVALID_ARGUMENT')
    category = category if category in _CATEGORIES else 'general'

    # 尽力解析店铺电话（解析失败不阻断工单登记本身）。
    # 修复（2026-10-09 端到端实测）：只有 shop_name 没有 shop_id 时，此前直接
    # query_shops('') 拉全量列表成功后就跳过了名称过滤，永远命中列表第一家
    # ——用户投诉「花诺花艺」却登记到「千百度花坊」。现在：名称必须走本地
    # 过滤；命中唯一一家才解析电话，命中多家/零家时电话置空，让模型向用户澄清。
    service_phone = None
    resolved_shop_name = shop_name
    resolved_shop_id = shop_id
    candidates: list = []
    if shop_id:
        result = await query_shops(shop_id, '')
        if result.get('ok') is True:
            candidates = (result.get('data') or {}).get('shops') or []
    elif shop_name:
        kw = shop_name.strip()
        by_name = await query_shops('', '')
        if by_name.get('ok') is True:
            candidates = [s for s in (by_name.get('data') or {}).get('shops') or []
                          if kw and kw in str(s.get('name') or '')]
    if len(candidates) == 1:
        service_phone = candidates[0].get('service_phone')
        resolved_shop_name = candidates[0].get('name') or shop_name
        resolved_shop_id = str(candidates[0].get('id') or shop_id or '')
    elif not shop_id and len(candidates) > 1:
        # 多家同名/子串命中：不猜，保留用户原话，电话留空。
        candidates = []

    ticket_id = None
    try:
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO merchant_tickets
                   (user_id, order_id, shop_id, shop_name, category, summary, detail, contact_phone)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                (user_id, order_id or None, resolved_shop_id or None, resolved_shop_name or None,
                 category, summary, detail or None, contact_phone or None))
            row = cur.fetchone()
            ticket_id = row['id'] if isinstance(row, dict) else row[0]
    except Exception:  # noqa: BLE001 - 落库失败要如实告知，不能谎报已登记
        return failure('INTERNAL_ERROR')

    # 提示词级防幻觉（2026-10-09 实测）：模型曾编造 0571-xxxx 电话写进回复。
    # service_phone 为 None 时明确说"未取到"，有值时强约束"只能使用这个号码"。
    if service_phone:
        phone_note = f'service_phone={service_phone} 是唯一可告知的店铺电话，回复中引用电话必须一字不差用它，禁止编造或拼凑其他号码。'
    else:
        phone_note = '未能解析到店铺电话，回复中不得编造任何电话号码；可建议用户在小程序门店页查看。'
    data = {'ticket_id': int(ticket_id), 'status': 'open', 'category': category,
            'order_id': order_id or None, 'shop_id': resolved_shop_id or None,
            'shop_name': resolved_shop_name or None,
            'service_phone': service_phone,
            'note': f'工单已登记，等待商家侧跟进。{phone_note}'}
    return {'ok': True, 'data': data}
