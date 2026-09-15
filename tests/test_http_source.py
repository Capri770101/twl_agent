"""平台 REST 只读数据源回归门（不发真实网络请求：打桩 _fetch_json）。

要点：
- 平台 **HTTP 恒 200**，业务码在 body 的 ``code`` —— 401/失败必须按 body 判断，别信状态码；
- 输出字段走**白名单**且**必须丢弃敏感字段**（证件照/商户号/分账比例）；
- 金额「分 → 元」与既有映射体系（cents_to_yuan）一致；
- ``order`` / ``user`` 平台未开放，必须明确报错而不是返回空。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data_gateway import http_source as hs


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('PLATFORM_API_AISTORE_URL', 'https://example.com')
    return 'aistore'


# ── 配置识别 ──

def test_source_ids_and_configured(configured):
    assert 'aistore' in hs.source_ids()
    assert hs.is_configured('aistore') is True
    assert hs.api_url('aistore') == 'https://example.com'


def test_not_configured_when_missing(monkeypatch):
    monkeypatch.delenv('PLATFORM_API_X_URL', raising=False)
    assert hs.is_configured('x') is False
    assert hs.api_url('x') == ''


def test_reject_non_http_url(monkeypatch):
    monkeypatch.setenv('PLATFORM_API_BAD_URL', 'postgresql://user:pw@host/db')
    assert hs.is_configured('bad') is False


def test_invalid_source_id_rejected():
    assert hs.api_url('a b') == ''
    assert hs.is_configured('../etc/passwd') is False


# ── 信封解包（关键：鉴权失败是 body 里的 code，不是 HTTP 401）──

def test_unwrap_list():
    rows = hs._unwrap({'code': 0, 'data': {'list': [{'id': 'a'}, {'id': 'b'}]}})
    assert [r['id'] for r in rows] == ['a', 'b']


def test_unwrap_plain_list():
    assert hs._unwrap({'code': 0, 'data': [{'id': 'a'}]}) == [{'id': 'a'}]


def test_unwrap_rejects_business_error():
    with pytest.raises(RuntimeError) as ei:
        hs._unwrap({'code': 401, 'message': '请先登录'})
    assert '请先登录' in str(ei.value)


# ── 字段白名单 + 金额换算 + 敏感字段丢弃 ──

def test_normalize_plan_cents_and_fields():
    raw = {
        'id': 'p1', 'name': '春风十里', 'price': 8800, 'originalPrice': 15800,
        'subtitle': '11枝粉玫瑰', 'ownerShopId': 's001', 'stock': 99,
        'flowerMeaning': '爱情', 'flowers': ['11枝粉玫瑰'], 'image': 'https://x/1.jpg',
        'unexpectedField': 'should-be-dropped',
    }
    row = hs.normalize_row('plan', raw)
    assert row['price'] == 88.0 and row['original_price'] == 158.0
    assert row['shop_id'] == 's001' and row['flower_meaning'] == '爱情'
    assert 'unexpectedField' not in row          # 白名单之外的字段不输出


def test_normalize_shop_drops_sensitive():
    raw = {
        'id': 's001', 'name': '千百度花坊', 'city': '舟山', 'businessHours': '07:00-22:00',
        'deliveryFee': 500, 'minOrderPrice': 3000, 'monthSales': 12,
        'subMchId': '1900000109', 'profitSharingRatio': 0.06,
        'idFront': 'https://x/idfront.jpg', 'idBack': 'https://x/idback.jpg',
        'license': 'https://x/license.jpg', 'phone': '13500000000',
    }
    row = hs.normalize_row('shop', raw)
    for leaked in ('sub_mch_id', 'profit_sharing_ratio', 'id_front', 'id_back', 'license'):
        assert leaked not in row
    assert not (hs.SENSITIVE_FIELDS & set(row)), '敏感字段不得出现在输出里'
    assert row['delivery_fee'] == 5.0 and row['min_order_price'] == 30.0
    assert row['business_hours'] == '07:00-22:00'


# ── 查询与本地过滤 ──

def _payload(rows):
    return {'code': 0, 'data': {'list': rows}}


def test_fetch_entity_filters_and_limits(configured, monkeypatch):
    rows = [
        {'id': 'p1', 'name': '粉玫瑰花束', 'price': 8800, 'ownerShopId': 's001'},
        {'id': 'p2', 'name': '向日葵花束', 'price': 5800, 'ownerShopId': 's002'},
        {'id': 'p3', 'name': '粉色康乃馨', 'price': 3900, 'ownerShopId': 's001'},
    ]
    monkeypatch.setattr(hs, '_fetch_json', lambda url, token='', timeout=hs.TIMEOUT: _payload(rows))

    out = hs.fetch_entity('aistore', 'plan', keyword='粉')
    assert [r['id'] for r in out] == ['p1', 'p3']

    out = hs.fetch_entity('aistore', 'plan', shop_id='s001')
    assert [r['id'] for r in out] == ['p1', 'p3']

    out = hs.fetch_entity('aistore', 'plan', row_id='p2')
    assert [r['id'] for r in out] == ['p2']

    out = hs.fetch_entity('aistore', 'plan', limit=1)
    assert len(out) == 1


def test_fetch_entity_hits_expected_url(configured, monkeypatch):
    seen = {}

    def fake(url, token='', timeout=hs.TIMEOUT):
        seen['url'] = url
        seen['token'] = token
        return _payload([])

    monkeypatch.setattr(hs, '_fetch_json', fake)
    hs.fetch_entity('aistore', 'shop')
    assert seen['url'].endswith('/v1/merchant/shops')


def test_fetch_entity_derives_open_status(configured, monkeypatch):
    """营业状态按「此刻」实时推算，与 DB 通路同一套派生字段。"""
    monkeypatch.setattr(hs, '_fetch_json', lambda *a, **k: _payload(
        [{'id': 's001', 'name': '店', 'businessHours': '00:00-23:59'}]
    ))
    out = hs.fetch_entity('aistore', 'shop')
    assert out[0]['is_open_now'] is True
    assert out[0]['open_status_text'] == '营业中'


def test_fetch_entity_unsupported_entity(configured):
    with pytest.raises(RuntimeError) as ei:
        hs.fetch_entity('aistore', 'order')
    assert 'order' in str(ei.value)


def test_fetch_entity_requires_config(monkeypatch):
    monkeypatch.delenv('PLATFORM_API_NOPE_URL', raising=False)
    with pytest.raises(RuntimeError):
        hs.fetch_entity('nope', 'plan')


def test_fetch_json_decodes_gbk_fallback(monkeypatch):
    """平台部分错误信息以 GBK 返回，不能因此崩掉。"""
    class _Resp:
        status_code = 200
        content = json.dumps({'code': 401, 'message': '请先登录'}, ensure_ascii=False).encode('gbk')

    import httpx

    monkeypatch.setattr(httpx, 'get', lambda *a, **k: _Resp())
    payload = hs._fetch_json('https://example.com/x')
    assert payload['code'] == 401


# ── 与既有查询入口的接线（DB 优先，未配库才落 HTTP）──

def test_query_external_entity_routes_to_http(configured, monkeypatch):
    from backend.data_gateway import external

    called = {}

    def fake_fetch(source_id, entity, **kw):
        called.update({'source_id': source_id, 'entity': entity, **kw})
        return [{'id': 'p1', 'name': 'x'}]

    monkeypatch.setattr(hs, 'fetch_entity', fake_fetch)
    rows = external.query_external_entity('aistore', 'plan', keyword='玫瑰', limit=5)
    assert rows == [{'id': 'p1', 'name': 'x'}]
    assert called['source_id'] == 'aistore' and called['entity'] == 'plan'


def test_query_external_entity_requires_mapping_without_http(monkeypatch):
    """既没配 HTTP 也没 active mapping 时，仍按原逻辑拒绝（行为不变）。"""
    from backend.data_gateway import external

    monkeypatch.delenv('PLATFORM_API_NONE_URL', raising=False)
    monkeypatch.setattr('backend.data_gateway.mapping_store.get_active_mapping', lambda sid: None)
    with pytest.raises(PermissionError):
        external.query_external_entity('none', 'plan')
