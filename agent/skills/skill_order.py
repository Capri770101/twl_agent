"""skills/skill_order.py —— 下单技能：调用「平台自有下单 API」。

背景（2026-09 重构，重要）：
- 智能体**不直写任何订单库**，本地 `orders` 表已随商品镜像一并移除；
- 商品 / 店铺来自平台数据库（``platform_db_query_entity``，强制只读）；
- 下单必须调用**平台方自己提供的下单接口**（由部署方向平台申请开通并配置环境变量），
  平台侧负责库存校验、履约与支付；智能体只负责组装订单信息并提交。

部署契约（需按平台 API 文档对齐，未配置时工具明确报错、绝不静默）：
- ``PLATFORM_ORDER_API_URL``：下单接口地址（必填）
- ``PLATFORM_ORDER_API_KEY``：可选；配置后以 ``Authorization: Bearer <key>`` 发送
- 请求：``POST {url}``，JSON body 见 :func:`_build_payload`
- 响应（期望 JSON，字段名不匹配时在 :func:`_parse_platform_order` 处对齐）::

    {"order_id": "P12345", "status": "created", "total_price": 198.0,
     "pay": {"type": "miniapp", "page_path": "...", "params": {...}}}
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from agent.tools import _resolve_session_plan, register_tool
from backend.config import settings

logger = logging.getLogger('skills.order')


def _now_compact() -> str:
    """YYYYMMDDHHMMSS 本地时间，用于生成幂等请求号前缀。"""
    return datetime.now().strftime('%Y%m%d%H%M%S')


def _plan_price(plan: dict) -> float:
    """方案估算金额：预设方案取 price；DIY 用 budget / budget_breakdown 兜底。

    注意：这只是估算值，最终金额以平台下单接口返回为准。
    """
    if isinstance(plan.get('price'), (int, float)) and plan['price'] > 0:
        return float(plan['price'])
    if isinstance(plan.get('budget_num'), (int, float)) and plan['budget_num'] > 0:
        return float(plan['budget_num'])
    if isinstance(plan.get('budget'), (int, float)) and plan['budget'] > 0:
        return float(plan['budget'])
    breakdown = plan.get('budget_breakdown') or {}
    if isinstance(breakdown.get('total_estimate'), (int, float)):
        return float(breakdown['total_estimate'])
    m = re.search(r'(\d+(?:\.\d+)?)', str(plan.get('estimated_price', '')))
    if m:
        return float(m.group(1))
    return 0.0


def _extract_flower_names(plan: dict) -> list[dict]:
    """从 DIY 方案中提取花材列表（带 qty）。

    返回 [{name, qty}] — qty 为该花材支数，缺失时默认 1。
    平台侧拿到花材需求清单后自行核对库存 / 替代方案。
    """
    design = plan.get('design') or {}
    flowers_raw = plan.get('flowers') or []
    seen: dict[str, int] = {}
    for key in ('main_flowers', 'fillers', 'foliage'):
        for f in design.get(key, []):
            if not isinstance(f, dict):
                continue
            name = f.get('name', '')
            if not name:
                continue
            qty = int(f.get('qty', 1)) if isinstance(f.get('qty'), (int, float)) else 1
            seen[name] = seen.get(name, 0) + qty
    for f in flowers_raw:
        if isinstance(f, dict) and f.get('name'):
            name = f['name']
            qty = int(f.get('qty', 1)) if isinstance(f.get('qty'), (int, float)) else 1
            seen[name] = seen.get(name, 0) + qty
    return [{'name': n, 'qty': q} for n, q in seen.items()]


def _build_items(plan: dict, plan_type: str) -> list[dict]:
    """组装订单明细 items（供平台侧参考；平台可自行换算/校验）。

    - existing（平台在售方案）：单条方案项，附 plan 的快照信息；
    - diy（智能体设计的花束）：花材需求清单 [{name, qty}] + 设计说明。
    """
    image = plan.get('effect_image_url') or plan.get('image') or plan.get('result_url') or ''
    if plan_type != 'diy':
        return [{
            'kind': 'plan',
            'plan_id': plan.get('plan_id') or plan.get('id') or '',
            'name': plan.get('name') or '花束',
            'qty': 1,
            'price': _plan_price(plan),
            'image': image,
        }]
    items: list[dict] = []
    for f in _extract_flower_names(plan):
        items.append({'kind': 'flower', 'name': f['name'], 'qty': f['qty']})
    if not items:
        items.append({'kind': 'plan', 'plan_id': plan.get('plan_id') or '', 'name': plan.get('name') or 'DIY 花束', 'qty': 1, 'price': _plan_price(plan), 'image': image})
    items.append({'kind': 'design_note', 'text': (plan.get('desc') or plan.get('meaning') or plan.get('diy_steps') or '')[:500]})
    return items


def _build_payload(plan: dict, shop_id: str, user_id: str, plan_type: str, session_id: str, request_id: str) -> dict[str, Any]:
    """按平台下单契约组装请求体。

    字段以通用命名为准；若平台 API 需要字段改名 / 嵌套，请在部署时扩展本函数。
    """
    image = plan.get('effect_image_url') or plan.get('image') or plan.get('result_url') or ''
    return {
        'request_id': request_id,  # 智能体侧幂等键，平台可用来去重
        'channel': 'flora_agent',
        'external_user_id': user_id,
        'agent_session_id': session_id,
        'shop_id': shop_id,
        'plan': {
            'plan_id': plan.get('plan_id') or plan.get('id') or '',
            'name': plan.get('name') or '未命名方案',
            'type': plan_type,  # existing | diy
            'price': _plan_price(plan),
            'desc': (plan.get('desc') or '')[:500],
            'image': image,
            'recipient': plan.get('recipient') or '',
            'occasion': plan.get('occasion') or '',
            'card_message': plan.get('card_message') or '',
        },
        'items': _build_items(plan, plan_type),
        'estimated_total': _plan_price(plan),  # 估算，最终以平台计算为准
        'remark': '来自花卉 DIY 智能体的订单请求',
    }


def _parse_platform_order(resp: dict) -> dict[str, Any]:
    """把平台下单响应规整为统一结构；字段名不匹配时在此对齐。"""
    order_id = str(resp.get('order_id') or resp.get('order_no') or resp.get('id') or resp.get('trade_id') or '')
    status = str(resp.get('status') or 'created')
    total = resp.get('total_price')
    if total is None:
        total = resp.get('total') or resp.get('amount') or resp.get('pay_amount') or 0
    try:
        total = float(total or 0)
    except (TypeError, ValueError):
        total = 0.0
    pay = resp.get('pay') if isinstance(resp.get('pay'), dict) else {}
    pay_url = str(resp.get('pay_url') or pay.get('url') or '')
    page_path = str(pay.get('page_path') or settings.pay_page_path)
    pay_params = pay.get('params') if isinstance(pay.get('params'), dict) else {}
    if pay_url:
        pay_params['pay_url'] = pay_url
    return {
        'order_id': order_id,
        'status': status,
        'total_price': total,
        'pay_jump': {'order_id': order_id, 'page_path': page_path, 'params': pay_params} if order_id else None,
    }


async def _submit_platform_order(payload: dict) -> dict[str, Any]:
    """POST 到平台下单接口，返回规整后的订单结果或抛出可读异常。"""
    url = (settings.PLATFORM_ORDER_API_URL or '').strip().rstrip('/')
    if not url:
        raise RuntimeError(
            '平台下单接口未配置：请部署方申请平台自有下单能力后，'
            '在环境变量中配置 PLATFORM_ORDER_API_URL（可选 PLATFORM_ORDER_API_KEY）。'
            '在此之前智能体无法代客下单，请引导用户前往平台/店铺完成下单。'
        )
    import httpx
    headers = {'Content-Type': 'application/json'}
    if (settings.PLATFORM_ORDER_API_KEY or '').strip():
        headers['Authorization'] = f"Bearer {settings.PLATFORM_ORDER_API_KEY.strip()}"
    timeout = httpx.Timeout(max(10.0, settings.REQUEST_TIMEOUT or 30.0))
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 400:
        body = resp.text[:500]
        raise RuntimeError(f'平台下单接口返回 HTTP {resp.status_code}: {body}')
    try:
        data = resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(f'平台下单接口响应非 JSON: {resp.text[:300]}')
    if not isinstance(data, dict):
        raise RuntimeError(f'平台下单接口响应格式异常: {str(data)[:300]}')
    err = data.get('error') or data.get('message')
    if err and (data.get('code') not in (None, 0, 200) or 'fail' in str(data.get('status', '')).lower()):
        raise RuntimeError(f'平台下单失败: {err}（原始返回: {str(data)[:300]}）')
    return data


# 2026-09-05 架构决定：关闭智能体「直接下单」能力（详见 MEMORY.md）。
# 订单改由客户在微信小程序侧点击商品卡片进入现有结算页完成（微信支付/分账/配送范围/订单导入复用平台现有逻辑）。
# 智能体只负责推荐结构化商品（reply + products 数组），不创建订单、不调用任何下单接口、不配置 PLATFORM_ORDER_API_URL。
# 如需恢复直连下单，取消下方注释即可（并同步恢复 agent.py system prompt 中「用户要买→create_order」的指引）。
# @register_tool(name='create_order', description='向用户确认后调用「平台自有下单 API」提交订单，生成支付跳转信息。智能体不写任何本地订单库：需部署方已配置 PLATFORM_ORDER_API_URL（平台下单接口），未配置时明确报错并引导用户去平台下单。会话从某店铺进入（已锁定店铺）时无需再查店铺列表，直接下单即可；此时若传入其他 shop_id 会被拒绝。', parameters={'type': 'object', 'properties': {'shop_id': {'type': 'string', 'description': '平台店铺 ID（来自平台查询返回的店铺 ID）。会话已锁定店铺时可省略，会自动使用锁定的店铺'}, 'plan_id': {'type': 'string', 'description': "方案引用：'latest' 表示会话当前方案；DIY_xxx 表示会话内 DIY 方案；平台在售方案 ID（需传 source_id）"}, 'plan_type': {'type': 'string', 'description': 'existing（平台在售方案）| diy（会话内 DIY 方案）'}, 'source_id': {'type': 'string', 'description': '可选：平台数据源 ID；plan_id 为平台在售方案时用来回读方案信息'}}, 'required': ['shop_id', 'plan_id', 'plan_type']}, inject_context=True, tags=['order'])
async def create_order(shop_id: str, plan_id: str, plan_type: str, source_id: str = '', _context: dict | None = None) -> str:
    """组装订单信息 → 调用平台自有下单 API → 返回 order_card / pay_jump 数据。"""
    user_id = (_context or {}).get('user_id', '')
    session_id = (_context or {}).get('session_id', '')
    if not user_id:
        return json.dumps({'error': '缺少用户身份，无法下单'}, ensure_ascii=False)
    # 会话从某家店铺进入时锁定该店：允许模型省略 shop_id，但禁止把单下到别家。
    locked_shop = str((_context or {}).get('shop_id') or '').strip()
    if locked_shop:
        if shop_id and str(shop_id) not in (locked_shop, 'first'):
            return json.dumps({'error': f'当前会话已锁定店铺 {locked_shop}，不能向其他店铺（{shop_id}）下单'}, ensure_ascii=False)
        shop_id = locked_shop
    if not shop_id or shop_id == 'first':
        return json.dumps({'error': '需要指定平台真实店铺 ID：请先用 platform_db_query_entity(entity="shop") 查询可用店铺并把 shop_id 传给我'}, ensure_ascii=False)
    if plan_type != 'diy':
        plan_type = 'existing'

    # ── 解析方案：会话内 DIY / 最近引用 → 会话；平台在售方案 → 只读回读 ──
    plan = await _resolve_session_plan(plan_id, _context)
    if plan is None and source_id and plan_id and plan_type == 'existing':
        try:
            from backend.data_gateway.external import query_external_entity
            # 下单回读：image 照常拼 CDN 前缀（订单卡片要显示图），
            # 但 price 必须保留平台原始「分」金额，绝不能转成「元」，
            # 否则平台下单金额会错乱（同事验收清单第 7 条：下单仍使用原始分金额）。
            rows = query_external_entity(source_id, 'plan', keyword=plan_id, limit=1, shop_id=shop_id, transform_fields=['image'])
            if rows:
                plan = rows[0]
        except Exception as exc:  # noqa: BLE001
            return json.dumps({'error': f'回读平台在售方案失败: {exc}'}, ensure_ascii=False)
    if not plan:
        return json.dumps({'error': '未找到可下单的方案：DIY 方案请先设计并确认（plan_id 用 latest），平台在售方案请用 platform_db_query_entity 查询后传回 source_id 与方案 ID'}, ensure_ascii=False)
    # 方案自带店铺归属时，锁定店铺下必须与之一致，避免拿别店的商品在本店下单。
    plan_shop = str(plan.get('shop_id') or '').strip()
    if locked_shop and plan_shop and plan_shop != locked_shop:
        return json.dumps({'error': f'该方案属于店铺 {plan_shop}，与当前锁定的店铺 {locked_shop} 不一致，无法下单'}, ensure_ascii=False)
    if plan_type == 'diy' and not plan.get('diy') and not str(plan.get('plan_id', '')).startswith('DIY_'):
        # 用户显式要下 DIY，但解析到的是平台商品 → 纠正类型
        if plan.get('price') is not None:
            plan_type = 'existing'

    # ── DIY 方案确认后落库（diy_plans，运行时数据，供后续生图/复用）──
    if plan_type == 'diy':
        try:
            from backend.storage.diy import save_diy_plan
            res = await save_diy_plan(plan, user_id)
            if res.get('plan_id') and not plan.get('plan_id'):
                plan['plan_id'] = res['plan_id']
        except Exception:  # noqa: BLE001
            logger.warning('[skill_order] DIY 方案落库失败（不影响提交平台）', exc_info=True)

    # ── 组装并提交平台 ──
    request_id = f"AG{_now_compact()}{uuid.uuid4().hex[:6]}"
    payload = _build_payload(plan, shop_id, user_id, plan_type, session_id, request_id)
    try:
        raw = await _submit_platform_order(payload)
    except RuntimeError as exc:
        logger.warning('[skill_order] 平台下单被拒: %s', exc)
        return json.dumps({'error': str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.exception('[skill_order] 调用平台下单接口异常')
        return json.dumps({'error': f'调用平台下单接口失败: {exc}'}, ensure_ascii=False)
    parsed = _parse_platform_order(raw)
    if not parsed['order_id']:
        logger.warning('[skill_order] 平台下单响应缺少订单号: %s', str(raw)[:300])
        return json.dumps({'error': f'平台已受理但响应缺少订单号，请稍后在平台侧查看（原始返回: {str(raw)[:300]}）'}, ensure_ascii=False)

    order_id = parsed['order_id']
    total = parsed['total_price'] or _plan_price(plan)
    logger.info('[skill_order] 订单已提交平台 user=%s shop=%s plan=%s order=%s total=%.2f status=%s', user_id, shop_id, plan.get('plan_id', ''), order_id, total, parsed['status'])
    return json.dumps({
        'order_id': order_id,
        'plan_name': plan.get('name', ''),
        'items': _build_items(plan, plan_type),
        'total_price': total,
        'plan_type': plan_type,
        'status': parsed['status'],
        'pay_jump': parsed['pay_jump'],
        'platform_response': {k: v for k, v in raw.items() if k not in ('pay',)},
        'effect_image_url': plan.get('effect_image_url') or plan.get('result_url') or '',
    }, ensure_ascii=False)
