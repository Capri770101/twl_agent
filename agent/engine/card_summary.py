"""卡片回复过短或被清理为空时的确定性用户可见兜底。"""
from __future__ import annotations

import logging
from typing import Any

from agent.engine.ui_protocol import UIType
from agent.tools_meta import CARD_UIS as _CARD_UIS

logger = logging.getLogger('agent')
_CARD_SUMMARY_MIN_REPLY = 30
_EMPTY_TEXT_FALLBACK = '抱歉，刚才那句我组织得不太清楚。你再说一下想要的风格或用途，我马上给你配一束～'
_EMPTY_CARD_FALLBACK = '我已经为你整理好相关结果啦，请查看下方卡片～'


def _ensure_card_summary(reply: str, ui: UIType, data: dict[str, Any]) -> str:
    """卡片回复过短时根据可信卡片字段追加可读要点。"""
    if ui not in (UIType.PLAN_CARD, UIType.SHOP_CARD, UIType.ORDER_CARD, UIType.PAY_JUMP, UIType.IMAGE_TASK, UIType.GREETING_CARD,
                  UIType.CUSTOMER_ORDERS, UIType.CUSTOMER_SHOPS, UIType.CUSTOMER_LOGIN):
        return reply
    if len((reply or '').strip()) >= _CARD_SUMMARY_MIN_REPLY:
        return reply
    base = (reply or '').strip()
    _simple = {
        UIType.ORDER_CARD: '订单信息已生成，确认无误就可以去支付啦～',
        UIType.PAY_JUMP: '订单信息已生成，确认无误就可以去支付啦～',
        UIType.IMAGE_TASK: '效果图正在生成中，稍等一下就好～',
        UIType.GREETING_CARD: '贺卡已经做好啦，看看喜欢吗？',
        UIType.CUSTOMER_LOGIN: '订单和售后信息需要登录后才能查询，点下方登录后回来继续问我就行～',
    }
    if ui in _simple:
        logger.info('[agent] 卡片回复过短，已自动追加要点（%s）', ui.value)
        return f'{base}\n\n{_simple[ui]}' if base else _simple[ui]
    if not isinstance(data, dict):
        return reply

    lines: list[str] = []
    if ui == UIType.PLAN_CARD:
        for plan in (data.get('plans') or [])[:4]:
            name = str((plan or {}).get('name') or '').strip()
            if not name:
                continue
            price = (plan or {}).get('price')
            lines.append(f'- {name}（{price:g} 元）' if isinstance(price, (int, float)) and price > 0 else f'- {name}')
        head = '给你挑的是这几款：'
    elif ui == UIType.CUSTOMER_SHOPS:
        shops = [s for s in (data.get('shops') or [])[:4] if isinstance(s, dict)]
        for shop in shops:
            name = str(shop.get('name') or '').strip()
            if not name:
                continue
            shop_bits: list[str] = []
            city = str(shop.get('city') or '').strip()
            if city:
                shop_bits.append(city)
            hours = str(shop.get('business_hours') or '').strip()
            if hours:
                shop_bits.append(hours)
            lines.append(f'- {name}' + (f'（{"、".join(shop_bits)}）' if shop_bits else ''))
        head = '查到这些门店：'
    elif ui == UIType.CUSTOMER_ORDERS:
        _status_label = {'pending': '待支付', 'paid': '已支付待接单', 'making': '制作中', 'delivering': '配送中',
                         'completed': '已完成', 'cancelled': '已取消', 'refund_applying': '退款审核中',
                         'refunding': '退款处理中', 'refunded': '已退款', 'refund_failed': '退款失败'}
        for order in (data.get('items') or [])[:4]:
            if not isinstance(order, dict) or not order.get('id'):
                continue
            oid = str(order['id'])
            shown = oid if len(oid) <= 22 else oid[:19] + '…'
            bits2: list[str] = []
            status = _status_label.get(str(order.get('status') or ''), '')
            if status:
                bits2.append(status)
            cents = order.get('total_price_cents')
            if isinstance(cents, (int, float)) and cents > 0:
                bits2.append(f'{cents / 100:g} 元')
            lines.append(f'- {shown}' + (f'（{"、".join(bits2)}）' if bits2 else ''))
        head = '查到这些订单：'
    else:
        for shop in (data.get('shops') or [])[:4]:
            name = str((shop or {}).get('name') or '').strip()
            if not name:
                continue
            bits: list[str] = []
            rating = (shop or {}).get('rating')
            if isinstance(rating, (int, float)) and rating > 0:
                bits.append(f'{rating:g} 分')
            price_range = str((shop or {}).get('price_range') or '').strip()
            if price_range:
                bits.append(price_range)
            lines.append(f'- {name}' + (f'（{"、".join(bits)}）' if bits else ''))
        head = '为你筛选了这几家：'

    if not lines:
        return reply
    summary = head + '\n' + '\n'.join(lines)
    base = (reply or '').strip()
    logger.info('[agent] 卡片回复过短，已自动追加要点（%d 条）', len(lines))
    return f'{base}\n\n{summary}' if base else summary


def _ensure_non_empty_reply(reply: str, ui: UIType) -> str:
    """清理链返回空文本时提供确定性兜底，避免前端空泡。"""
    if (reply or '').strip():
        return reply
    logger.warning('[agent] 清理链后回复为空（ui=%s），已使用确定性兜底', ui.value)
    return _EMPTY_CARD_FALLBACK if ui in _CARD_UIS else _EMPTY_TEXT_FALLBACK
