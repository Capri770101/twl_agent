"""从工具结果提取可信平台事实与明确未接通的写操作证据。"""
from __future__ import annotations

import json
from typing import Any


def _platform_queried(tool_log: list[Any], entity: str) -> bool:
    """本轮是否成功查询过目标平台实体。"""
    for tc in tool_log or []:
        if getattr(tc, 'name', '') != 'platform_db_query_entity':
            continue
        if getattr(tc, 'status', '') != 'ok':
            continue
        if str((getattr(tc, 'arguments', None) or {}).get('entity') or '') == entity:
            return True
    return False


def _platform_rows_found(tool_log: list[Any], entity: str) -> bool:
    """目标实体的成功平台查询是否实际返回了非空记录。"""
    for tc in tool_log or []:
        if (getattr(tc, 'name', '') != 'platform_db_query_entity'
                or getattr(tc, 'status', '') != 'ok'
                or (getattr(tc, 'arguments', None) or {}).get('entity') != entity):
            continue
        try:
            result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result
        except (ValueError, TypeError):
            continue
        if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('data'), list) and result['data']:
            return True
    return False


def _unavailable_write_action(tool_log: list[Any]) -> str:
    """只信真实工具返回的 CHANNEL_UNAVAILABLE，不能信模型话术。"""
    actions = {'cancel_my_order': '取消订单', 'apply_refund': '申请退款',
               'update_order_address': '修改收货地址'}
    for tc in reversed(tool_log or []):
        if getattr(tc, 'name', '') not in actions:
            continue
        try:
            raw = getattr(tc, 'result', None)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(result, dict) and result.get('code') == 'CHANNEL_UNAVAILABLE':
            return actions[tc.name]
    return ''


def _local_merchant_ticket(tool_log: list[Any]) -> dict:
    """识别真实工具记录的本地工单，不能当作已投递商家。"""
    for tc in reversed(tool_log or []):
        if getattr(tc, 'name', '') not in ('contact_merchant', 'query_my_tickets'):
            continue
        try:
            raw = getattr(tc, 'result', None)
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get('ok') is True:
            data = payload.get('data') or {}
            if isinstance(data, dict) and data.get('delivery_scope') == 'local_only':
                return data
            if isinstance(data, dict) and isinstance(data.get('items'), list):
                items = data['items']
                if items and all(isinstance(item, dict) and item.get('delivery_scope') == 'local_only' for item in items):
                    return {'ticket_id': None, 'delivery_scope': 'local_only'}
    return {}
