import asyncio
import httpx
import pytest
from backend.customer_service import query_orders
from backend.config import settings


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', 'https://business.example/api/customer-service')


def test_read_only_fixed_url_and_field_whitelist():
    def handle(req):
        assert req.method == 'GET'
        assert req.url.host == 'business.example'
        assert 'user_id' not in req.url.params
        assert req.headers['authorization'] == 'Bearer cs1.test.signature'
        return httpx.Response(200, json={'ok': True, 'data': {'items': [{'id': 'o1', 'status': 'refund_applying', 'phone': 'private'}], 'has_more': False}})
    result = asyncio.run(query_orders('cs1.test.signature', transport=httpx.MockTransport(handle)))
    assert result['data']['items'] == [{'id': 'o1', 'status': 'refund_applying'}]


@pytest.mark.parametrize('status,code', [(401,'AUTH_REQUIRED'),(403,'FORBIDDEN'),(404,'NOT_FOUND'),(429,'RATE_LIMITED'),(500,'UPSTREAM_UNAVAILABLE'),(302,'UPSTREAM_UNAVAILABLE')])
def test_failure_is_not_empty_order_list(status, code):
    result = asyncio.run(query_orders('cs1.test.signature', transport=httpx.MockTransport(lambda r: httpx.Response(status))))
    assert result['code'] == code
    assert 'data' not in result


def test_missing_identity_and_path_injection_do_not_connect():
    assert asyncio.run(query_orders(''))['code'] == 'AUTH_REQUIRED'
    assert asyncio.run(query_orders('cs1.test.signature', order_id='../admin'))['code'] == 'INVALID_ARGUMENT'


def test_http_destination_refused(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', 'http://business.example')
    assert asyncio.run(query_orders('cs1.test.signature'))['code'] == 'UNSUPPORTED'


def test_timeout():
    def handle(req): raise httpx.ReadTimeout('do not leak credential')
    result = asyncio.run(query_orders('cs1.test.signature', transport=httpx.MockTransport(handle)))
    assert result == {'ok': False, 'code': 'UPSTREAM_TIMEOUT', 'retryable': True}


def test_after_sales_preserves_review_and_money_state_separately():
    def handle(req):
        assert req.url.path.endswith('/orders/o1/after-sales')
        return httpx.Response(200, json={'ok': True, 'data': {'id': 'o1', 'status': 'refunding',
            'after_sales': {'state': 'processing', 'merchant_review': 'approved', 'internal_note': 'private'}}})
    result = asyncio.run(query_orders('cs1.test.signature', order_id='o1', after_sales=True, transport=httpx.MockTransport(handle)))
    assert result['data']['after_sales']['state'] == 'processing'
    assert 'internal_note' not in result['data']['after_sales']
