"""learning router —— 平台真实订单 → 智能体 proven 域（L2 成交即学）。

契约（需部署方与平台对齐，未配置密钥时端点返回 503，fail-closed）：
- 平台在真实订单落定时，POST https://<host>/api/learning/order
- 请求头：X-Learning-Key: <LEARNING_WEBHOOK_SECRET>
- 请求体（JSON，二选一）：
    { "plan_id": "DIY_abc123" }                                  # 已知 DIY / 智能体方案 → order_count+1
    { "occasion": "生日", "style": "韩式", "flowers": ["玫瑰"],    # 平台方案特征 → 哈希生成 proven 条目
      "recipient": "妈妈", "budget": 300, "color_scheme": ["粉"],
      "packaging": "雾面纸", "meaning": "生日快乐", "count": 1 }
- 响应：{ "status": "ok", "action": "order_bumped" | "proven_imported", "plan_id": "..." }

安全边界（红线）：本端点只写入 proven 域（diy_plans），不调 LLM、不改代码 / 工具 / 提示词
（L4 自改代码为禁止项）。密钥缺失即拒绝，避免 proven 域被裸奔污染。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from backend.config import settings
from backend.storage.diy import ingest_order_signal

logger = logging.getLogger('routers.learning')

router = APIRouter(prefix='/api/learning', tags=['learning'])


def _authenticate(x_learning_key: str | None) -> None:
    """校验学习回调密钥；缺失或不匹配即拒绝（fail-closed）。"""
    expected = (settings.LEARNING_WEBHOOK_SECRET or '').strip()
    if not expected:
        raise HTTPException(status_code=503, detail='学习回调未配置 LEARNING_WEBHOOK_SECRET，拒绝接收订单信号')
    if x_learning_key != expected:
        raise HTTPException(status_code=403, detail='invalid learning key')


@router.post('/order')
async def order_signal(
    request: Request,
    x_learning_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """平台真实订单回调：把成交方案计入 proven 域，实现「成交即学」。

    这是 L2「成交即学」的**真实成交钩子**，与 agent.py 内的会话确认钩子（record_plan_confirmed）
    互补：前者是用户说「就这个」的即时信号，本端点是平台真实付款落单的权威信号。
    """
    _authenticate(x_learning_key)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail='请求体必须是合法 JSON')
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='请求体必须是 JSON 对象')
    try:
        result = ingest_order_signal(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info('[learning] 订单信号已入库 action=%s plan_id=%s', result.get('action'), result.get('plan_id'))
    return {'status': 'ok', **result}
