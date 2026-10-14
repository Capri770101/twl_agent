import asyncio
import json
from unittest.mock import AsyncMock
from agent import customer_tools
from agent.toolkit import TOOL_REGISTRY, visible_tool_specs
from backend.config import settings


def test_credential_only_from_context(monkeypatch):
    call = AsyncMock(return_value={'ok': False, 'code': 'AUTH_REQUIRED'})
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = asyncio.run(customer_tools.query_my_orders('order-1', _context={'customer_credential': 'cs1.private'}))
    call.assert_awaited_once_with('cs1.private', order_id='order-1', page=1)
    assert 'private' not in result
    assert json.loads(result)['code'] == 'AUTH_REQUIRED'
    props = TOOL_REGISTRY['query_my_orders'].parameters['properties']
    assert not {'user_id', 'credential', 'token'} & props.keys()


def test_after_sales_uses_only_explicit_user_order_id(monkeypatch):
    call = AsyncMock(return_value={'ok': True, 'data': {'after_sales': {'state': 'pending_review', 'amount_cents': 15900}}})
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_my_after_sales(
        'ORD-20260928-7741', _context={'user_message': '订单 TLOCAL2026100807 的退款审核通过了吗？',
                                       'customer_credential': 'cs1.private'})))
    assert result['ok'] is True
    call.assert_awaited_once_with('cs1.private', order_id='TLOCAL2026100807', after_sales=True)
    assert '原样' in TOOL_REGISTRY['query_my_after_sales'].parameters['properties']['order_id']['description']


def test_after_sales_multiple_order_ids_requires_clarification(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    result = json.loads(asyncio.run(customer_tools.query_my_after_sales(
        'TLOCAL2026100807', _context={'user_message': '订单 TLOCAL2026100807 和 TLOCAL2026100811 哪笔退款了？'})))
    assert result['code'] == 'INVALID_ARGUMENT'
    call.assert_not_awaited()


def test_after_sales_without_explicit_id_preserves_tool_argument(monkeypatch):
    call = AsyncMock(return_value={'ok': False, 'code': 'NOT_FOUND'})
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_my_after_sales(
        'TLOCAL2026100811', _context={'user_message': '这笔退款怎么样了？', 'customer_credential': 'cs1.private'})))
    assert result['code'] == 'NOT_FOUND'
    call.assert_awaited_once_with('cs1.private', order_id='TLOCAL2026100811', after_sales=True)


def test_after_sales_does_not_treat_dates_as_order_ids(monkeypatch):
    call = AsyncMock(return_value={'ok': True, 'data': {}})
    monkeypatch.setattr(customer_tools, 'query_orders', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    asyncio.run(customer_tools.query_my_after_sales(
        'wrong-id', _context={'user_message': '2026-10-08 订单号 ORD-20261008-1234 退款了吗？',
                              'customer_credential': 'cs1.private'}))
    call.assert_awaited_once_with('cs1.private', order_id='ORD-20261008-1234', after_sales=True)


def test_feature_off_hides_private_tool(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', False)
    assert 'query_my_orders' not in {s.name for s in visible_tool_specs()}


def test_write_tools_hidden_until_channel_configured(monkeypatch):
    """写通道未配置时，取消/退款/改址不出现在工具面（fail-closed）。"""
    import asyncio
    from agent.toolkit import execute_tool
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_WRITE_ENABLED', False)
    names = {s.name for s in visible_tool_specs()}
    assert not {'cancel_my_order', 'apply_refund', 'update_order_address'} & names
    # 执行层兜底：模型硬调也被拒
    result, status = asyncio.run(execute_tool('apply_refund', {'order_id': 'X1'}, {}))
    assert status == 'error' and '暂未开放' in result
    # 配置后恢复可见（客服总开关 + 写通道开关都需开启）
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_WRITE_ENABLED', True)
    names2 = {s.name for s in visible_tool_specs()}
    assert {'cancel_my_order', 'apply_refund', 'update_order_address'} <= names2


def _shop(name: str, sid: str = 's001', city: str = '福州') -> dict:
    return {'id': sid, 'name': name, 'city': city, 'address': 'x', 'business_hours': '08:00-22:00',
            'delivery_policy': None, 'service_phone': None, 'updated_at': None}


def test_shop_name_keyword_falls_back_to_local_filter(monkeypatch):
    """模型把中文店名当 shop_id 传（线上实测高发）→ 本地按名过滤，不再 INVALID_ARGUMENT。

    回归背景（2026-10-08 部署后实测）：提示词宣传「店铺编号或名称关键词」，
    模型传「花诺花艺」→ 后端 ASCII 正则拒收 → 模型连续重试直至迭代耗尽，
    用户只收到「思考得太久」空壳。
    """
    call = AsyncMock(return_value={'ok': True, 'data': {'shops': [_shop('花诺花艺', 's007'), _shop('千百度花坊', 's001')]},
                                   'fetched_at': 't', 'source': 'shop_backend'})
    monkeypatch.setattr(customer_tools, 'query_shops', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_shop_service(shop_id='花诺花艺', _context={})))
    assert result['ok'] is True
    assert [s['name'] for s in result['data']['shops']] == ['花诺花艺']
    assert result['data']['filtered_by_name'] == '花诺花艺'
    # 降级路径按列表查询（不带编号），城市照常透传（本例未传城市则为空）
    call.assert_awaited_once_with('', '')


def test_shop_name_keyword_passes_city(monkeypatch):
    """关键词降级时城市参数照常透传（缩小候选范围）。"""
    call = AsyncMock(return_value={'ok': True, 'data': {'shops': [_shop('花诺花艺', 's007', '福州')]}, 'fetched_at': 't'})
    monkeypatch.setattr(customer_tools, 'query_shops', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_shop_service(shop_id='花诺花艺', city='福州', _context={})))
    call.assert_awaited_once_with('', '福州')
    assert result['data']['shops'][0]['name'] == '花诺花艺'


def test_shop_name_keyword_no_match_is_honest_empty(monkeypatch):
    """关键词未命中 → 如实空列表（模型据此说没找到，不猜测）。"""
    call = AsyncMock(return_value={'ok': True, 'data': {'shops': [_shop('千百度花坊')]}, 'fetched_at': 't'})
    monkeypatch.setattr(customer_tools, 'query_shops', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_shop_service(shop_id='不存在的店', _context={})))
    assert result['ok'] is True and result['data']['shops'] == []


def test_real_shop_id_still_uses_direct_path(monkeypatch):
    """真实编号（ASCII 形态）仍走 /shops/<id> 精确查询，不受降级逻辑影响。"""
    call = AsyncMock(return_value={'ok': True, 'data': {'shops': [_shop('花诺花艺', 's007')]}})
    monkeypatch.setattr(customer_tools, 'query_shops', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    asyncio.run(customer_tools.query_shop_service(shop_id='s007', _context={}))
    call.assert_awaited_once_with('s007', '')


def test_keyword_fallback_propagates_upstream_failure(monkeypatch):
    """降级路径中上游失败（如 UNSUPPORTED）→ 原样透传错误，不当成空结果。"""
    call = AsyncMock(return_value={'ok': False, 'code': 'UNSUPPORTED', 'retryable': False})
    monkeypatch.setattr(customer_tools, 'query_shops', call)
    monkeypatch.setattr(customer_tools, 'record_customer_query', lambda *args: None)
    result = json.loads(asyncio.run(customer_tools.query_shop_service(shop_id='花诺花艺', _context={})))
    assert result == {'ok': False, 'code': 'UNSUPPORTED', 'retryable': False}
