import asyncio
import httpx
from backend.customer_shops import query_shops
from backend.config import settings


def test_shop_query_no_private_credential_and_no_defaults(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', 'https://business.example/api/customer-service')
    def handle(req):
        assert 'authorization' not in req.headers
        return httpx.Response(200, json={'ok': True, 'data': {'id': 's1', 'name': '花店', 'subMchId': 'secret'}})
    result = asyncio.run(query_shops('s1', transport=httpx.MockTransport(handle)))
    assert result['data']['shops'][0]['business_hours'] is None
    assert 'subMchId' not in result['data']['shops'][0]


def test_wrong_shop_not_silently_substituted(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', 'https://business.example/api/customer-service')
    result = asyncio.run(query_shops('s1', transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'ok': True, 'data': {'id': 's2'}}))))
    assert result['code'] == 'INVALID_UPSTREAM_RESPONSE'
