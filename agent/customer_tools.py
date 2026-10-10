"""只读客户服务工具；凭据由执行上下文注入，绝不由模型填写。"""
import json
import re
import time
from backend.observability import record_customer_query
from agent.toolkit import register_tool
from backend.customer_service import query_orders
from backend.customer_shops import query_shops
from backend.merchant_contact import contact_merchant as _contact_merchant
from backend.customer_actions import (query_shop_reviews as _query_shop_reviews,
                                      cancel_my_order as _cancel_my_order,
                                      apply_refund as _apply_refund,
                                      update_order_address as _update_order_address,
                                      channel_unavailable as _channel_unavailable)

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


@register_tool(name='contact_merchant', description='用户明确要求联系商家/店家、投诉、催单或转人工时调用：登记一条商家联系单（工单）并返回店铺电话。summary 用一句话概括诉求；detail 填用户原话；有订单号必传 order_id。不要在用户没要求时主动调用。回复中提到电话时必须一字不差使用工具返回的 service_phone，未返回电话就明说没有，禁止编造号码。',
               parameters={'type': 'object', 'properties': {
                   'order_id': {'type': 'string', 'description': '关联订单号，无则留空'},
                   'shop_id': {'type': 'string', 'description': '店铺编号（字母数字）'},
                   'shop_name': {'type': 'string', 'description': '店名关键词（中文店名传这里）'},
                   'category': {'type': 'string', 'enum': ['general', 'order', 'after_sales', 'custom', 'complaint'], 'description': '诉求类别'},
                   'summary': {'type': 'string', 'description': '诉求一句话摘要（≤200字）'},
                   'detail': {'type': 'string', 'description': '用户诉求原话（≤2000字）'},
                   'contact_phone': {'type': 'string', 'description': '用户愿意留的回拨电话，未提供留空'}},
                   'required': ['summary'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def contact_merchant(order_id: str = '', shop_id: str = '', shop_name: str = '', category: str = 'general',
                           summary: str = '', detail: str = '', contact_phone: str = '', _context: dict | None = None) -> str:
    user_id = (_context or {}).get('user_id') or ''
    if not user_id:
        return json.dumps({'ok': False, 'code': 'AUTH_REQUIRED'}, ensure_ascii=False)
    shop_id = shop_id or (_context or {}).get('shop_id') or ''
    if shop_id == 'default':
        shop_id = ''
    result = await _tracked('contact_merchant', _contact_merchant(
        user_id, order_id=order_id, shop_id=shop_id, shop_name=shop_name,
        category=category, summary=summary, detail=detail, contact_phone=contact_phone,
        credential=(_context or {}).get('customer_credential', '')))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='query_shop_reviews', description='查询某门店的用户评价（星级、内容、日期）。用户问「这家店评价怎么样/口碑如何」时调用；total=0 时如实说暂无评价数据，不要编造评价内容。',
               parameters={'type': 'object', 'properties': {
                   'shop_id': {'type': 'string', 'description': '店铺编号（字母数字）'},
                   'shop_name': {'type': 'string', 'description': '店名关键词（中文店名传这里）'},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'description': '最多返回几条，默认5'}},
                   'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def query_shop_reviews(shop_id: str = '', shop_name: str = '', limit: int = 5, _context: dict | None = None) -> str:
    if not shop_id:
        shop_id = (_context or {}).get('shop_id') or ''
    if shop_id == 'default':
        shop_id = ''
    selected = (shop_id or '').strip()
    if selected and not _SHOP_ID_RE.match(selected):
        # 店名关键词：拉候选列表解析成编号再查评价
        shops_result = await _tracked('shops', query_shops('', ''))
        if shops_result.get('ok') is True:
            kw = selected.strip()
            matched = [s for s in (shops_result.get('data') or {}).get('shops') or []
                       if kw and kw in str(s.get('name') or '')]
            if len(matched) == 1:
                selected = str(matched[0].get('id') or '')
            else:
                return json.dumps({'ok': True, 'data': {'total': 0, 'reviews': [],
                    'note': f'按名称「{kw}」匹配到 {len(matched)} 家店，请让用户确认具体门店'}}, ensure_ascii=False)
        else:
            selected = ''
    if not selected:
        return json.dumps({'ok': True, 'data': {'total': 0, 'reviews': [],
            'note': '未指定店铺；请先确认用户想查哪家店'}}, ensure_ascii=False)
    result = await _tracked('shop_reviews', _query_shop_reviews(selected, limit=limit))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='cancel_my_order', description='取消本人订单（仅「待接单/pending」状态可取消）。⚠️写操作：必须先向用户确认（复述订单号）得到肯定答复后才调用。当前后端写通道暂未打通（返回 CHANNEL_UNAVAILABLE）：如实告知该渠道暂未打通，建议用户在小程序订单页取消或帮登记联系商家工单，不要说取消成功。',
               parameters={'type': 'object', 'properties': {
                   'order_id': {'type': 'string', 'description': '要取消的订单号'}},
                   'required': ['order_id'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def cancel_my_order(order_id: str, _context: dict | None = None) -> str:
    result = await _tracked('cancel_order', _cancel_my_order(
        (_context or {}).get('customer_credential', ''), order_id))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='apply_refund', description='为本人订单提交退款申请（写操作）。⚠️必须先确认订单号和退款原因，得到肯定答复后才调用。当前后端写通道暂未打通（CHANNEL_UNAVAILABLE）：如实告知并建议小程序订单页申请或登记商家工单；不要假装退款已提交。',
               parameters={'type': 'object', 'properties': {
                   'order_id': {'type': 'string', 'description': '要退款的订单号'},
                   'reason': {'type': 'string', 'description': '退款原因（用户原话，≤255字）'},
                   'amount_cents': {'type': 'integer', 'minimum': 1, 'description': '退款金额（分）；不传默认整单'}},
                   'required': ['order_id'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def apply_refund(order_id: str, reason: str = '', amount_cents: int = 0, _context: dict | None = None) -> str:
    result = await _tracked('apply_refund', _apply_refund(
        (_context or {}).get('customer_credential', ''), order_id, reason, amount_cents))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='update_order_address', description='修改本人订单的收货地址。当前后端通道暂未打通：调用后返回 CHANNEL_UNAVAILABLE 时，如实告知用户该渠道暂未打通，建议在小程序订单页修改或联系商家。',
               parameters={'type': 'object', 'properties': {
                   'order_id': {'type': 'string', 'description': '要改地址的订单号'},
                   'address': {'type': 'object', 'properties': {
                       'name': {'type': 'string'}, 'phone': {'type': 'string'},
                       'region': {'type': 'string', 'description': '省市区'}, 'detail': {'type': 'string', 'description': '详细地址'}},
                       'description': '新地址'}},
                   'required': ['order_id', 'address'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def update_order_address(order_id: str, address: dict | None = None, _context: dict | None = None) -> str:
    result = await _tracked('update_address', _update_order_address(
        (_context or {}).get('customer_credential', ''), order_id, address or {}))
    return json.dumps(result, ensure_ascii=False, default=str)


@register_tool(name='set_reminder', description='为用户设置周期提醒（如「每月8号提醒我给妈订花」「后天下午提醒我取花」）。提醒到点写入站内通知收件箱；外部小程序推送尚需接入方接通。用户明确要求记住某个日期/周期性事项时调用；模糊请求先澄清日期。',
               parameters={'type': 'object', 'properties': {
                   'content': {'type': 'string', 'description': '提醒内容（用户原话概括）'},
                   'cron': {'type': 'string', 'description': '5字段 cron（分 时 日 月 周），如 "0 9 8 * *" 表示每月8号9点'},
                   'first_run': {'type': 'string', 'description': '首次触发时间 YYYY-MM-DD HH:MM（一次性提醒传这个）'},
                   'repeat': {'type': 'boolean', 'description': 'true=按 cron 周期触发，false=一次性'}},
                   'required': ['content'], 'additionalProperties': False},
               inject_context=True, tags=['customer_service'])
async def set_reminder(content: str, cron: str = '', first_run: str = '', repeat: bool = False, _context: dict | None = None) -> str:
    user_id = (_context or {}).get('user_id') or ''
    if not user_id:
        return json.dumps({'ok': False, 'code': 'AUTH_REQUIRED'}, ensure_ascii=False)
    content = re.sub(r'[\r\n\t]+', ' ', str(content or '')).strip()[:200]
    if not content:
        return json.dumps({'ok': False, 'code': 'INVALID_ARGUMENT'}, ensure_ascii=False)
    cron = str(cron or '').strip()
    first_run = str(first_run or '').strip()
    if repeat:
        # cron 是 5 个空格分隔字段（如 "0 9 8 * *"）——校验交给 backend.reminders.parse_cron，
        # 工具层不再用无空格正则误拒（2026-10-10 实测：模型传 "0 9 8 * *" 被拒 3 次直至超时）。
        from backend.reminders import parse_cron as _parse_cron
        if _parse_cron(cron) is None:
            return json.dumps({'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': 'cron 需为 5 字段（分 时 日 月 周），如 0 9 8 * *'}, ensure_ascii=False)
    elif not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', first_run):
        return json.dumps({'ok': False, 'code': 'INVALID_ARGUMENT', 'reason': 'first_run 需为 YYYY-MM-DD HH:MM'}, ensure_ascii=False)
    from backend.reminders import create_reminder
    result = await _tracked('set_reminder', create_reminder(
        user_id, content, cron=cron if repeat else '', first_run=first_run if not repeat else '', repeat=repeat))
    return json.dumps(result, ensure_ascii=False, default=str)
