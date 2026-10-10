"""本人事项查询必须凭 JWT，并且只看得见自己的本地工单。"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.storage import db
from main import app


@pytest.fixture
def client(monkeypatch):
    conn = getattr(db._thread_local, 'conn', None)
    if conn is not None:
        conn._conn.close()
        db._thread_local.conn = None
    monkeypatch.setattr(settings, 'DATABASE_URL',
                        'postgresql://postgres@127.0.0.1:5433/flora_agent_test')
    monkeypatch.setattr(settings, 'AUTH_REQUIRED', True)
    monkeypatch.setattr(settings, 'JWT_SECRET', 'test-key-' + uuid.uuid4().hex)
    db.init_db()
    with db.transaction() as conn:
        conn.execute('DELETE FROM merchant_tickets')
    yield TestClient(app)
    with db.transaction() as conn:
        conn.execute('DELETE FROM merchant_tickets')
    conn = getattr(db._thread_local, 'conn', None)
    if conn is not None:
        conn._conn.close()
        db._thread_local.conn = None


def _auth(user_id):
    from backend.auth import create_access_token
    return {'Authorization': 'Bearer ' + create_access_token(user_id)}


def _insert(user_id, summary, detail='诉求详情', phone='13800000000'):
    with db.transaction() as conn:
        return conn.execute(
            'INSERT INTO merchant_tickets (user_id, summary, detail, contact_phone) '
            'VALUES (?, ?, ?, ?) RETURNING id',
            (user_id, summary, detail, phone)).fetchone()['id']


def test_ticket_list_and_detail_are_owner_only(client):
    mine = _insert('owner', '催问配送')
    other = _insert('other', '他人的退款诉求', detail='隐私详情', phone='13900000000')
    response = client.get('/api/merchant-tickets', headers=_auth('owner')).json()
    assert response['ok'] and len(response['data']['items']) == 1
    item = response['data']['items'][0]
    assert item['id'] == str(mine) and 'detail' not in item and 'contact_phone' not in item
    assert item['delivery_scope'] == 'local_only' and item['merchant_delivery_verified'] is False
    detail = client.get(f'/api/merchant-tickets/{mine}', headers=_auth('owner')).json()['data']
    assert detail['detail'] == '诉求详情' and detail['contact_phone'] == '13800000000'
    assert client.get(f'/api/merchant-tickets/{other}', headers=_auth('owner')).status_code == 404
    assert client.get('/api/merchant-tickets', headers=_auth('other')).json()['data']['items'][0]['id'] == str(other)


def test_ticket_endpoints_reject_anonymous_and_bad_identity(client):
    _insert('owner', '已登记')
    assert client.get('/api/merchant-tickets').status_code == 401
    assert client.get('/api/merchant-tickets/1').status_code == 401
    assert client.get('/api/merchant-tickets', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    assert client.get('/api/merchant-tickets', headers=_auth('owner'), params={'limit': -5}).json()['data']['limit'] == 1
    assert client.get('/api/merchant-tickets', headers=_auth('owner'), params={'offset': 999999}).json()['data']['offset'] == 10000


def test_chat_tool_uses_context_identity_and_never_reports_delivery(client):
    import asyncio
    import json
    from agent.customer_tools import query_my_tickets
    mine = _insert('owner', '询问发货')
    other = _insert('other', '他人的投诉')
    listed = json.loads(asyncio.run(query_my_tickets(_context={'user_id': 'owner', 'ticket_access_verified': True})))
    assert listed['ok'] is True and [x['id'] for x in listed['data']['items']] == [str(mine)]
    assert listed['data']['items'][0]['merchant_delivery_verified'] is False
    denied = json.loads(asyncio.run(query_my_tickets(other, _context={'user_id': 'owner', 'ticket_access_verified': True})))
    assert denied['code'] == 'NOT_FOUND'
    invalid = json.loads(asyncio.run(query_my_tickets('oops', _context={'user_id': 'owner', 'ticket_access_verified': True})))
    assert invalid['code'] == 'INVALID_ARGUMENT'
    stale_context = json.loads(asyncio.run(query_my_tickets(_context={'user_id': 'owner'})))
    assert stale_context['code'] == 'AUTH_REQUIRED'
    no_identity = json.loads(asyncio.run(query_my_tickets(_context={})))
    assert no_identity['code'] == 'AUTH_REQUIRED'


def test_ticket_history_rejects_old_but_still_valid_jwt(client):
    import jwt
    import time
    _insert('owner', '个人联系方式', detail='私密详情')
    now = int(time.time())
    old_token = jwt.encode({'sub': 'owner', 'iat': now - 16 * 60, 'exp': now + 3600},
                           settings.JWT_SECRET, algorithm='HS256')
    headers = {'Authorization': 'Bearer ' + old_token}
    assert client.get('/auth/me', headers=headers).status_code == 200
    assert client.get('/api/merchant-tickets', headers=headers).status_code == 401
    assert client.get('/api/merchant-tickets/1', headers=headers).status_code == 401


def test_chat_ticket_permission_requires_fresh_same_user_token(client):
    import jwt
    import time
    from starlette.requests import Request
    from backend.routers.chat import _fresh_ticket_access
    now = int(time.time())
    def request(token):
        return Request({'type': 'http', 'headers': [(b'authorization', ('Bearer ' + token).encode())]})
    fresh = _auth('owner')['Authorization'].split(' ', 1)[1]
    assert _fresh_ticket_access(request(fresh), 'owner') is True
    assert _fresh_ticket_access(request(fresh), 'other') is False
    old = jwt.encode({'sub': 'owner', 'iat': now - 16 * 60, 'exp': now + 3600},
                     settings.JWT_SECRET, algorithm='HS256')
    assert _fresh_ticket_access(request(old), 'owner') is False


def test_ticket_requires_identity_even_when_dev_auth_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, 'AUTH_REQUIRED', False)
    assert client.get('/api/merchant-tickets').status_code == 401
