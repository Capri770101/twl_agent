"""售后动作适配器：指令理解 → 工具调用 → 后端通道未打通时如实兜底。

职责边界（2026-10-10 与产品确认的架构约定）：
- 智能体负责**意图理解与调用**：把用户口语诉求解析成结构化动作，
  调 H5 已有端点执行或查询；
- 后端通道不存在/未打通 → 返回 CHANNEL_UNAVAILABLE，让模型如实告知
  用户「该渠道暂未打通」，**绝不假装执行成功**；
- 写操作（取消订单/申请退款）必须先经用户确认——工具本身不做确认
  （那是模型对话层的事），但执行结果如实透传，包括失败原因。

端点现状（本地联调实测 2026-10-10）：
- 评价：GET /api/shops/{id}/reviews —— 存在；READ_SOURCE=api 模式恒空
  （aistore 源暂无评价端点）→ 空数据如实说「暂无评价数据」。
- 取消订单 / 申请退款：POST /api/orders/{id}/cancel|refund —— **通道
  未打通**：这两个端点用 H5 用户 JWT 鉴权（浏览器会话态），而智能体
  服务端只持有 cs1 票据（scope=orders:read，只读）。这不是缺陷而是
  安全设计——写操作通道需要 H5 在 /api/customer-service 下新增
  写端点（票据验签 + scope 扩展 orders:write）后才能接通。在那之前
  如实返回 CHANNEL_UNAVAILABLE，引导用户在小程序订单页操作或走
  contact_merchant 工单。
- 改地址：H5 无端点 → CHANNEL_UNAVAILABLE。

⚠️ 代码里的 _h5_post 调用路径保留（等 H5 补写端点后切换 base 到
customer-service 路由即可），但当前 cancel/apply_refund 直接返回
CHANNEL_UNAVAILABLE，不再向用户态端点发注定 401 的请求。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from backend.config import settings
from backend.customer_service import failure

_ORDER_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')
_SHOP_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


def _base() -> str:
    # H5 根地址 = CUSTOMER_SERVICE_BASE_URL 去掉 /api/customer-service 尾部。
    base = settings.CUSTOMER_SERVICE_BASE_URL.rstrip('/')
    for tail in ('/api/customer-service', '/customer-service'):
        if base.endswith(tail):
            return base[: -len(tail)]
    return base


def _transport_ok() -> bool:
    """HTTPS 生产 / 本地 dev 放行 HTTP（与 customer_service 同一约定）。"""
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return False
    parsed = urlsplit(_base())
    _allow_http = settings.CUSTOMER_SERVICE_ALLOW_INSECURE and settings.APP_ENV.lower() in {'dev', 'development', 'local', 'test'}
    return parsed.scheme == 'https' or (_allow_http and parsed.scheme == 'http')


async def _h5_post(path: str, credential: str, body: dict) -> tuple[int, dict | None]:
    """带 cs1 票据调 H5 写端点，返回 (status, json)。网络/解析异常抛出由调用方兜。"""
    url = _base() + path
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        r = await client.post(url, json=body, headers={'Authorization': 'Bearer ' + credential})
    try:
        payload = r.json()
    except ValueError:
        payload = None
    return r.status_code, payload


async def _h5_get(path: str) -> tuple[int, dict | list | None]:
    url = _base() + path
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        r = await client.get(url)
    try:
        payload = r.json()
    except ValueError:
        payload = None
    return r.status_code, payload


async def query_shop_reviews(shop_id: str, *, limit: int = 5) -> dict:
    """查门店评价（公开端点）。空数据 → ok=True, reviews=[]，如实说没有。

    H5 READ_SOURCE=api 模式下该端点恒返回 []（aistore 源无评价数据），
    这属于「通道存在但暂无数据」，不是错误——模型话术由调用方组织。
    """
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    if not _transport_ok():
        return failure('UNSUPPORTED')
    shop_id = str(shop_id or '').strip()
    if not _SHOP_ID_RE.fullmatch(shop_id):
        return failure('INVALID_ARGUMENT')
    limit = max(1, min(int(limit or 5), 10))
    try:
        status, payload = await _h5_get(f'/api/shops/{shop_id}/reviews')
        if status == 404:
            return failure('NOT_FOUND')
        if status != 200:
            return failure('UPSTREAM_UNAVAILABLE', status >= 500)
        rows = payload if isinstance(payload, list) else []
        reviews = [{'user': str(r.get('user') or '用户')[:20],
                    'rating': r.get('rating'),
                    'content': str(r.get('content') or '')[:200],
                    'date': str(r.get('date') or '')[:20]}
                   for r in rows[:limit] if isinstance(r, dict)]
        return {'ok': True, 'data': {'shop_id': shop_id, 'total': len(rows),
                                     'reviews': reviews},
                'source': 'h5_reviews'}
    except httpx.TimeoutException:
        return failure('UPSTREAM_TIMEOUT', True)
    except (httpx.HTTPError, ValueError, TypeError):
        return failure('UPSTREAM_UNAVAILABLE', True)


async def cancel_my_order(credential: str, order_id: str) -> dict:
    """取消订单——通道暂未打通（写操作超 cs1 只读 scope），如实兜底。

    H5 的 /api/orders/:id/cancel 用用户 JWT 鉴权（浏览器会话态），
    服务端智能体只有只读票据。等 H5 在 customer-service 契约下补
    写端点（orders:write scope）后，把下面的返回换成真实调用即可。
    """
    if not isinstance(credential, str) or not credential.startswith('cs1.'):
        return failure('AUTH_REQUIRED')
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    order_id = str(order_id or '').strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        return failure('INVALID_ARGUMENT')
    return channel_unavailable('cancel_my_order')


async def apply_refund(credential: str, order_id: str, reason: str = '', amount_cents: int = 0) -> dict:
    """申请退款——通道暂未打通（同 cancel：写操作超 cs1 只读 scope）。

    H5 的 /api/orders/:id/refund 涉及资金操作且用用户 JWT 鉴权，
    服务端票据无权发起。等 H5 在 customer-service 契约下补写端点后
    切换为真实调用（body 契约见函数内注释保留）。
    """
    if not isinstance(credential, str) or not credential.startswith('cs1.'):
        return failure('AUTH_REQUIRED')
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    order_id = str(order_id or '').strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        return failure('INVALID_ARGUMENT')
    return channel_unavailable('apply_refund')


def channel_unavailable(action: str) -> dict:
    """改地址等 H5 未提供端点的动作 → 统一兜底码（模型据此如实告知）。"""
    return {'ok': False, 'code': 'CHANNEL_UNAVAILABLE', 'action': str(action)[:40]}


async def update_order_address(credential: str, order_id: str, address: dict) -> dict:
    """改订单收货地址——H5 暂无对应端点（2026-10-10 盘点）。

    保留函数与工具形态：后端将来补 PATCH /api/orders/:id/address 后，
    只需把这里的返回换成真实调用，对话层零改动。
    """
    if not isinstance(credential, str) or not credential.startswith('cs1.'):
        return failure('AUTH_REQUIRED')
    if not settings.CUSTOMER_SERVICE_ENABLED:
        return failure('UNSUPPORTED')
    order_id = str(order_id or '').strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        return failure('INVALID_ARGUMENT')
    return channel_unavailable('update_order_address')
