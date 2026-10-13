"""售后回复仅以本轮成功、唯一的本人订单查询为事实来源。"""
from __future__ import annotations

import json
import re
from typing import Any

_STATE_TEXT = {
    'pending_review': '退款申请仍在审核中，尚未确认通过，也未证明退款到账',
    'processing': '退款正在处理中，尚未证明到账',
    'succeeded': '退款状态显示已完成',
    'failed': '退款状态显示失败',
    'rejected': '商家审核未通过，款项尚未退回',
    'none': '当前没有查到已提交的售后申请',
    'unknown': '售后状态暂未确认',
}
_ORDER_ID = re.compile(r'(?<![\w-])(?:TLOCAL[\w-]+|ORD[-_][\w-]+|\d{12,})(?![\w-])')


def align_after_sales_reply(reply: str, tool_log: list[Any]) -> str:
    """只在唯一成功的指定售后查询上纠偏；失败/多笔时不捏造事实。"""
    results = []
    for record in tool_log:
        if record.name != 'query_my_after_sales' or record.status != 'ok':
            continue
        try:
            payload = json.loads(record.result) if isinstance(record.result, str) else record.result
        except (TypeError, ValueError):
            continue
        data = payload.get('data') if isinstance(payload, dict) and payload.get('ok') is True else None
        if isinstance(data, dict) and data.get('id') and isinstance(data.get('after_sales'), dict):
            results.append(data)
    ids = {str(data['id']) for data in results}
    if len(ids) != 1:
        return reply
    data = results[-1]
    order_id = str(data['id'])
    after_sales = data['after_sales']
    state = str(after_sales.get('state') or 'unknown')
    amount = after_sales.get('amount_cents')
    amount_text = ''
    if isinstance(amount, int) and amount > 0:
        amount_text = f'，申请金额 {amount / 100:g} 元'
    fact = f'订单 {order_id} 的售后状态：{_STATE_TEXT.get(state, _STATE_TEXT["unknown"])}{amount_text}。'
    # 模型捏造编号/查不到/到账/状态冲突时，用可信数据重建整句，避免残留矛盾说法。
    mentioned = set(_ORDER_ID.findall(reply or ''))
    if (mentioned - {order_id} or order_id not in (reply or '')
            or any(w in reply for w in ('没查到', '查不到', '订单号不对'))
            or (state == 'pending_review' and any(w in reply for w in ('已到账', '审核通过', '已退款成功')))):
        return fact
    if amount_text and not re.search(rf'(?<!\d){amount / 100:g}\s*元', reply):
        return reply.rstrip() + f' 这笔退款的申请金额为 {amount / 100:g} 元。'
    return reply
