"""contact_merchant 工具与商家联系单回归测试。

覆盖：
- 正常登记（含店铺解析 → service_phone 回填）；
- 店名关键词降级（模型把中文名传进 shop_id）；
- 名称唯一命中/多命中不猜/零命中保留原话（2026-10-09 端到端回归）；
- 参数校验（summary 必填、订单号格式、电话格式、类别兜底）；
- 未启用客服时 UNSUPPORTED；落库失败如实 INTERNAL_ERROR。

DB 策略：与其他存储测试一致——默认给 settings 注入专用测试库 URL
（本机 5433/flora_agent_test），不依赖环境变量在 import 前就绪
（settings 是模块级单例，先跑的其他测试会固定其值，setdefault 无效）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import merchant_contact as mc  # noqa: E402
from backend.customer_service import failure  # noqa: E402



def _reset_thread_conn():
    from backend.storage import db as _db
    conn = getattr(_db._thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        _db._thread_local.conn = None

@pytest.fixture()
def pg(monkeypatch):
    _reset_thread_conn()
    """注入测试库 URL + 启用客服；真库建表（幂等），每例前清空工单表。"""
    monkeypatch.setattr(mc.settings, 'DATABASE_URL',
                        'postgresql://postgres@127.0.0.1:5433/flora_agent_test')
    monkeypatch.setattr(mc.settings, 'CUSTOMER_SERVICE_ENABLED', True)
    from backend.storage import db
    db.init_db()
    with db.transaction() as conn:
        conn.execute('DELETE FROM merchant_tickets')
    yield
    _reset_thread_conn()
    with db.transaction() as conn:
        conn.execute('DELETE FROM merchant_tickets')


def _shop(shop_id='s001', name='千百度花坊', phone='13567675068'):
    return {'ok': True, 'data': {'shops': [{
        'id': shop_id, 'name': name, 'city': '舟山', 'address': 'x',
        'business_hours': '07:00-22:00', 'delivery_policy': None,
        'service_phone': phone, 'updated_at': None}]}, 'source': 'shop_backend'}


def _run(coro):
    return asyncio.run(coro)


def test_normal_ticket_with_phone(pg, monkeypatch):
    async def fake_shops(shop_id, city, **kw):
        return _shop() if shop_id == 's001' else failure('NOT_FOUND')
    monkeypatch.setattr(mc, 'query_shops', fake_shops)
    r = _run(mc.contact_merchant('u1', shop_id='s001', order_id='TLOCAL2026100807',
                                category='after_sales', summary='花蔫了要求退款',
                                detail='收到的花不新鲜', contact_phone='13900001001'))
    assert r['ok'] is True
    d = r['data']
    assert d['status'] == 'open' and d['service_phone'] == '13567675068'
    assert d['shop_name'] == '千百度花坊' and d['ticket_id'] > 0
    assert d['order_id'] == 'TLOCAL2026100807'


def test_shop_name_fallback_when_id_is_chinese(pg, monkeypatch):
    """模型把店名传进 shop_id → 降级为名称解析，单店命中回填电话。"""
    calls = {'by_id': 0, 'list': 0}

    async def fake_shops(shop_id, city, **kw):
        if shop_id:
            calls['by_id'] += 1
            return failure('INVALID_ARGUMENT')  # 中文不是合法 id
        calls['list'] += 1
        return _shop()
    monkeypatch.setattr(mc, 'query_shops', fake_shops)
    r = _run(mc.contact_merchant('u1', shop_id='千百度花坊', summary='催单'))
    assert r['ok'] is True
    assert r['data']['service_phone'] == '13567675068'
    assert r['data']['shop_name'] == '千百度花坊'
    assert calls['list'] == 1


def test_shop_name_only_resolves_exact_match(pg, monkeypatch):
    """只传 shop_name（无 id）→ 必须按名称过滤；不得错拿全列表第一家。

    回归（2026-10-09 端到端实测）：用户投诉「花诺花艺」却登记到列表第一家
    「千百度花坊」。修复后：唯一命中回填电话；多家命中不猜、电话留空。
    """
    two = {'ok': True, 'data': {'shops': [
        {'id': 's001', 'name': '千百度花坊', 'service_phone': '13567675068'},
        {'id': 's013', 'name': '花诺花艺', 'service_phone': '18650717901'},
    ]}}

    async def fake_shops(shop_id, city, **kw):
        assert shop_id == ''
        return two
    monkeypatch.setattr(mc, 'query_shops', fake_shops)
    r = _run(mc.contact_merchant('u1', shop_name='花诺花艺', summary='投诉'))
    assert r['ok'] is True
    assert r['data']['shop_name'] == '花诺花艺'
    assert r['data']['service_phone'] == '18650717901'
    assert r['data']['shop_id'] == 's013'


def test_shop_name_multi_match_no_guess(pg, monkeypatch):
    """名称子串命中多家 → 不猜电话，工单照常登记。"""
    multi = {'ok': True, 'data': {'shops': [
        {'id': 's001', 'name': '花诺花艺（福州店）', 'service_phone': '1'},
        {'id': 's002', 'name': '花诺花艺（厦门店）', 'service_phone': '2'},
    ]}}

    async def fake_shops(shop_id, city, **kw):
        return multi
    monkeypatch.setattr(mc, 'query_shops', fake_shops)
    r = _run(mc.contact_merchant('u1', shop_name='花诺花艺', summary='x'))
    assert r['ok'] is True
    assert r['data']['service_phone'] is None
    assert r['data']['shop_name'] == '花诺花艺'  # 保留用户原话


def test_shop_name_no_match_keeps_user_text(pg, monkeypatch):
    async def none_matched(shop_id, city, **kw):
        return _shop()  # 只有千百度，用户要的是别家
    monkeypatch.setattr(mc, 'query_shops', none_matched)
    r = _run(mc.contact_merchant('u1', shop_name='不存在花店', summary='x'))
    assert r['ok'] is True
    assert r['data']['service_phone'] is None
    assert r['data']['shop_name'] == '不存在花店'


def test_summary_required(pg):
    r = _run(mc.contact_merchant('u1', summary='   '))
    assert r == failure('INVALID_ARGUMENT')


def test_bad_order_id_rejected(pg):
    r = _run(mc.contact_merchant('u1', order_id='x; drop table orders', summary='恶意'))
    assert r == failure('INVALID_ARGUMENT')


def test_bad_phone_rejected(pg):
    r = _run(mc.contact_merchant('u1', summary='x', contact_phone='abc-def'))
    assert r == failure('INVALID_ARGUMENT')


def test_bad_category_falls_back_to_general(pg, monkeypatch):
    async def no_shops(shop_id, city, **kw):
        return failure('NOT_FOUND')
    monkeypatch.setattr(mc, 'query_shops', no_shops)
    r = _run(mc.contact_merchant('u1', shop_id='s001', category='随便什么', summary='咨询'))
    assert r['ok'] is True
    assert r['data']['category'] == 'general'
    assert r['data']['service_phone'] is None  # 解析不到电话不阻断


def test_disabled_when_customer_service_off(pg, monkeypatch):
    monkeypatch.setattr(mc.settings, 'CUSTOMER_SERVICE_ENABLED', False)
    r = _run(mc.contact_merchant('u1', summary='x'))
    assert r == failure('UNSUPPORTED')


def test_db_failure_is_honest(pg, monkeypatch):
    async def no_shops(shop_id, city, **kw):
        return failure('NOT_FOUND')
    monkeypatch.setattr(mc, 'query_shops', no_shops)
    import contextlib

    @contextlib.contextmanager
    def boom():
        raise RuntimeError('pg down')
        yield
    monkeypatch.setattr(mc, 'transaction', boom)
    r = _run(mc.contact_merchant('u1', summary='x'))
    assert r == failure('INTERNAL_ERROR')


def test_user_id_required():
    # 空 user_id 不落库（参数校验先于 DB），但 settings 必须合法：
    # 显式指向测试库并启用客服，避免其他测试固化 settings 的影响。
    _reset_thread_conn()
    orig = (mc.settings.DATABASE_URL, mc.settings.CUSTOMER_SERVICE_ENABLED)
    mc.settings.DATABASE_URL = 'postgresql://postgres@127.0.0.1:5433/flora_agent_test'
    mc.settings.CUSTOMER_SERVICE_ENABLED = True
    try:
        r = _run(mc.contact_merchant('', summary='x'))
        assert r == failure('AUTH_REQUIRED')
    finally:
        mc.settings.DATABASE_URL, mc.settings.CUSTOMER_SERVICE_ENABLED = orig


def test_ticket_rows_persisted(pg, monkeypatch):
    async def no_shops(shop_id, city, **kw):
        return failure('NOT_FOUND')
    monkeypatch.setattr(mc, 'query_shops', no_shops)
    for i in range(3):
        r = _run(mc.contact_merchant(f'u{i}', summary=f'诉求{i}'))
        assert r['ok'] is True
    from backend.storage import db
    with db.transaction() as conn:
        n = conn.execute('SELECT COUNT(*) AS n FROM merchant_tickets').fetchone()['n']
    assert n == 3
