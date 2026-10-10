"""Authentication issuance limits remain active even when chat rate limiting is off."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.routers import auth as routes
from backend.rate_limit import FixedWindowLimiter


def _request(ip: str, forwarded: str = '') -> Request:
    headers = [(b'x-real-ip', forwarded.encode())] if forwarded else []
    return Request({'type': 'http', 'headers': headers, 'client': (ip, 1234)})


def test_auth_limiter_does_not_trust_forwarded_ip_or_chat_switch(monkeypatch):
    monkeypatch.setattr(routes, '_auth_limiter', FixedWindowLimiter(2))
    monkeypatch.setattr(routes.settings, 'RATE_LIMIT_ENABLED', False)
    routes._limit_auth_request(_request('203.0.113.7', '1.1.1.1'))
    routes._limit_auth_request(_request('203.0.113.7', '2.2.2.2'))
    with pytest.raises(HTTPException) as caught:
        routes._limit_auth_request(_request('203.0.113.7', '3.3.3.3'))
    assert caught.value.status_code == 429
    assert int(caught.value.headers['Retry-After']) >= 1
    routes._limit_auth_request(_request('203.0.113.8'))


def test_anonymous_limiter_is_stricter(monkeypatch):
    monkeypatch.setattr(routes, '_anonymous_limiter', FixedWindowLimiter(1))
    routes._limit_auth_request(_request('203.0.113.9'), anonymous=True)
    with pytest.raises(HTTPException) as caught:
        routes._limit_auth_request(_request('203.0.113.9'), anonymous=True)
    assert caught.value.status_code == 429


def test_anonymous_endpoint_rejects_second_issuance(monkeypatch):
    monkeypatch.setattr(routes, '_anonymous_limiter', FixedWindowLimiter(1))
    monkeypatch.setattr(routes.settings, 'ANONYMOUS_LOGIN_ENABLED', True)
    monkeypatch.setattr(routes.settings, 'JWT_SECRET', '9fa60db4c860f61e4da8709448e14c2a7b61b2064c6a3468')
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    assert client.post('/auth/anonymous').status_code == 200
    assert client.post('/auth/anonymous').status_code == 429


def test_invalid_api_keys_are_limited_before_key_check(monkeypatch):
    monkeypatch.setattr(routes, '_auth_limiter', FixedWindowLimiter(1))
    monkeypatch.setattr(routes.settings, 'PLATFORM_API_KEYS', 'h5app=a-strong-configured-platform-key')
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)
    assert client.post('/auth/token', json={'external_user_id': 'u'}, headers={'X-API-Key': 'wrong'}).status_code in (401, 403)
    assert client.post('/auth/token', json={'external_user_id': 'u'}, headers={'X-API-Key': 'wrong'}).status_code == 429
