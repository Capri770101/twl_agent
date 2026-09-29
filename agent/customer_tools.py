"""只读客户服务工具；凭据由执行上下文注入，绝不由模型填写。"""
import json
import time
from backend.observability import record_customer_query
from agent.toolkit import register_tool
from backend.customer_service import query_orders
from backend.customer_shops import query_shops


async def _tracked(operation, call):
    started = time.perf_counter()
    code = 'INTERNAL_ERROR'
    try:
        result = await call
        code = 'SUCCESS' if result.get('ok') is True else result.get('code', 'UNKNOWN')
        return result
    finally:
        record_customer_query(operation, code, int((time.perf_counter() - started) * 1000))


@register_tool(name='query_shop_service', description='查询真实门店地址、营业时间和配送说明；无需登录。未指定店铺且无会话店铺时列候选，请用户选择。空字段表示未提供，不得猜测营业或配送承诺。',
               parameters={'type': 'object', 'properties': {'shop_id': {'type': 'string'}, 'city': {'type': 'string'}}, 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def query_shop_service(shop_id: str = '', city: str = '', _context: dict | None = None) -> str:
    selected = shop_id or (_context or {}).get('shop_id') or ''
    if selected == 'default': selected = ''
    return json.dumps(await _tracked('shops', query_shops(selected, city)), ensure_ascii=False, default=str)


@register_tool(name='query_my_orders', description='查询当前已登录客户本人的订单列表或指定订单状态。多笔订单先请用户选择；AUTH_REQUIRED 时引导登录，其他失败不得说成没有订单。只读，不能取消或退款。',
               parameters={'type': 'object', 'properties': {'order_id': {'type': 'string', 'description': '用户选定的订单编号；留空列出订单'}, 'page': {'type': 'integer', 'minimum': 1, 'maximum': 10000}}, 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def query_my_orders(order_id: str = '', page: int = 1, _context: dict | None = None) -> str:
    result = await _tracked('orders', query_orders((_context or {}).get('customer_credential', ''), order_id=order_id or None, page=page))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='query_my_after_sales', description='查询本人指定订单的售后审核及退款进度。必须先确定订单；商家审核通过不代表退款到账。只读，不提交退款申请。',
               parameters={'type': 'object', 'properties': {'order_id': {'type': 'string'}}, 'required': ['order_id'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def query_my_after_sales(order_id: str, _context: dict | None = None) -> str:
    result = await _tracked('after_sales', query_orders((_context or {}).get('customer_credential', ''), order_id=order_id, after_sales=True))
    return json.dumps(result, ensure_ascii=False, default=str)
