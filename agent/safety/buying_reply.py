"""在售商品没有可信查询结果时不发布模型编造的商品或不存在的卡片。"""
from __future__ import annotations

import json
from typing import Any


_INTENT_DESCRIPTION = {
    'gratitude': '感谢', 'apology': '歉意', 'affection': '爱意', 'comfort': '陪伴与安慰',
    'encouragement': '鼓励', 'celebration': '祝贺', 'remembrance': '思念',
}


def missing_product_reply(tool_log: list[Any], message: str = '') -> str | None:
    """有真实在售行就让原流程回答；没有时返回明确的查询边界。"""
    attempted = False
    for record in tool_log:
        if record.name != 'platform_db_query_entity' or (record.arguments or {}).get('entity') != 'plan':
            continue
        attempted = True
        try:
            payload = json.loads(record.result) if isinstance(record.result, str) else record.result
        except (TypeError, ValueError):
            continue
        if record.status == 'ok' and isinstance(payload, dict) and payload.get('ok') is True and payload.get('data'):
            return None
    if not attempted:
        return None
    from agent.design.gift_intent import understand_gift_message
    intent, _ = understand_gift_message(message)
    feeling = _INTENT_DESCRIPTION.get(intent or '')
    understood = f'我理解你希望向收花人表达{feeling}。' if feeling else ''
    return (understood + '目前没查到可核实的在售花束和价格，不能凭花语推断商品花材、库存或给出具体购买款式。'
            '可以稍后重试商品查询，或明确告诉我是否需要定制方案。')
