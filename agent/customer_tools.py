"""只读客户服务工具；凭据由执行上下文注入，绝不由模型填写。"""
import json
import re
import time
from backend.observability import record_customer_query
from agent.toolkit import register_tool
from backend.customer_service import query_orders
from backend.customer_shops import query_shops

#: H5 契约里的门店编号形态（见 docs/customer-service-contract.md §3）。
#: 不匹配时说明模型把**店名**当编号传了（线上实测高发：提示词写了
#: 「店铺编号或名称关键词」，模型自然传中文名）——后端会拒
#: INVALID_ARGUMENT，模型收到后往往连续重试直至迭代耗尽，用户只看到
#: 「思考得太久」空壳。工具层按名称关键词降级：拉列表→本地过滤。
_SHOP_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


async def _tracked(operation, call):
    started = time.perf_counter()
    code = 'INTERNAL_ERROR'
    try:
        result = await call
        code = 'SUCCESS' if result.get('ok') is True else result.get('code', 'UNKNOWN')
        return result
    finally:
        record_customer_query(operation, code, int((time.perf_counter() - started) * 1000))


async def _shops_by_name_keyword(keyword: str, city: str) -> dict:
    """店名关键词降级查询：契约无 name 参数，拉候选列表后本地过滤。

    - 命中 1 家 → 卡片直接给这家；
    - 命中多家 → 全部返回，模型请用户选择；
    - 命中 0 家 → 空列表 + filtered_by_name，模型如实说没找到（不猜）。
    """
    result = await _tracked('shops', query_shops('', city))
    if result.get('ok') is not True:
        return result
    shops = (result.get('data') or {}).get('shops') or []
    kw = keyword.strip()
    matched = [s for s in shops if kw and kw in str(s.get('name') or '')]
    return {'ok': True, 'data': {'shops': matched, 'filtered_by_name': kw},
            'fetched_at': result.get('fetched_at'), 'source': 'shop_backend'}


@register_tool(name='query_shop_service', description='查询真实门店地址、营业时间和配送说明；无需登录。未指定店铺且无会话店铺时列候选，请用户选择。空字段表示未提供，不得猜测营业或配送承诺。',
               parameters={'type': 'object', 'properties': {
                   'shop_id': {'type': 'string', 'description': '门店编号（字母数字）或店名关键词（中文店名直接传这里也可以）'},
                   'city': {'type': 'string', 'description': '城市名，仅在用户提到城市时传'}},
                   'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def query_shop_service(shop_id: str = '', city: str = '', _context: dict | None = None) -> str:
    selected = shop_id or (_context or {}).get('shop_id') or ''
    if selected == 'default': selected = ''
    if selected and not _SHOP_ID_RE.match(selected.strip()):
        return json.dumps(await _shops_by_name_keyword(selected, city), ensure_ascii=False, default=str)
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
