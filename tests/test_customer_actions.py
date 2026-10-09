"""售后动作（取消/退款/改地址/评价/提醒）回归测试。

架构约定（2026-10-10 与产品确认）：智能体做指令理解与工具调用；
后端通道未打通 → CHANNEL_UNAVAILABLE 如实兜底，绝不假装成功。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import customer_actions as ca  # noqa: E402
from backend.customer_service import failure  # noqa: E402
from backend.reminders import parse_cron, _next_after  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── cron 解析与下次触发 ──────────────────────────────────────────

def test_cron_valid_and_invalid():
    assert parse_cron('0 9 8 * *') is not None
    assert parse_cron('30 9 * * 1') is not None
    assert parse_cron('*/5 * * * *') is not None
    assert parse_cron('0-30 9 1,15 * *') is not None
    assert parse_cron('x') is None
    assert parse_cron('0 9 8 *') is None          # 4 字段
    assert parse_cron('99 9 * * *') is None       # 分钟越界
    assert parse_cron('0 25 8 * *') is None       # 小时越界
    assert parse_cron('* * * * * *') is None      # 6 字段


def test_cron_dom_dow_or_semantics():
    """day 与 weekday 都受限时 OR（POSIX 语义）；周一 9:30 的下次触发。"""
    parsed = parse_cron('30 9 * * 1')
    base = _dt.datetime(2026, 10, 8, 8, 0)  # 周四
    nxt = _next_after(parsed, base)
    assert nxt is not None and nxt.weekday() == 0 and (nxt.hour, nxt.minute) == (9, 30)
    # 2026-10-12 是周一（从周四 10-08 起的第一个周一）
    assert nxt.strftime('%Y-%m-%d %H:%M') == '2026-10-12 09:30'


def test_cron_monthly_8th():
    parsed = parse_cron('0 9 8 * *')
    base = _dt.datetime(2026, 10, 9, 10, 0)
    nxt = _next_after(parsed, base)
    assert nxt == _dt.datetime(2026, 11, 8, 9, 0)


# ── 评价查询（公开端点；空数据如实） ────────────────────────────

def test_query_shop_reviews_transport_guard(monkeypatch):
    monkeypatch.setattr(ca.settings, 'CUSTOMER_SERVICE_ENABLED', False)
    r = _run(ca.query_shop_reviews('s001'))
    assert r == failure('UNSUPPORTED')


def test_query_shop_reviews_bad_id(monkeypatch):
    _write_env(monkeypatch)
    r = _run(ca.query_shop_reviews('s00; drop'))
    assert r == failure('INVALID_ARGUMENT')


def test_query_shop_reviews_happy_path(monkeypatch):
    async def fake_get(path):
        return 200, [{'user': '张**', 'rating': 5, 'content': '花很新鲜', 'date': '2026-10-01'},
                     {'user': '李**', 'rating': 4, 'content': '配送快', 'date': '2026-09-28'}]
    monkeypatch.setattr(ca, '_h5_get', fake_get)
    _write_env(monkeypatch)
    r = _run(ca.query_shop_reviews('s001'))
    assert r['ok'] is True
    assert r['data']['total'] == 2 and len(r['data']['reviews']) == 2
    assert r['data']['reviews'][0]['content'] == '花很新鲜'


def test_query_shop_reviews_empty_is_ok(monkeypatch):
    async def fake_get(path):
        return 200, []
    monkeypatch.setattr(ca, '_h5_get', fake_get)
    _write_env(monkeypatch)
    r = _run(ca.query_shop_reviews('s001'))
    assert r['ok'] is True and r['data']['reviews'] == []


def _write_env(monkeypatch):
    """写操作三件套共用环境（reviews 的用例也复用）。"""
    from backend.config import settings
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ENABLED', True)
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_ALLOW_INSECURE', True)
    monkeypatch.setattr(settings, 'APP_ENV', 'dev')
    monkeypatch.setattr(settings, 'CUSTOMER_SERVICE_BASE_URL', 'http://127.0.0.1:4000/api/customer-service')


# ── 取消订单 / 申请退款：通道未打通（写操作超 cs1 只读 scope） ──


def test_cancel_channel_unavailable(monkeypatch):
    """写操作通道未打通 → 如实 CHANNEL_UNAVAILABLE（不发注定 401 的请求）。"""
    _write_env(monkeypatch)
    r = _run(ca.cancel_my_order('cs1.abc', 'TLOCAL2026100801'))
    assert r['ok'] is False and r['code'] == 'CHANNEL_UNAVAILABLE'
    assert r['action'] == 'cancel_my_order'


def test_cancel_still_validates(monkeypatch):
    _write_env(monkeypatch)
    assert _run(ca.cancel_my_order('', 'T1')) == failure('AUTH_REQUIRED')
    assert _run(ca.cancel_my_order('nope', 'T1')) == failure('AUTH_REQUIRED')
    assert _run(ca.cancel_my_order('cs1.x', 'T; drop')) == failure('INVALID_ARGUMENT')


def test_refund_channel_unavailable(monkeypatch):
    _write_env(monkeypatch)
    r = _run(ca.apply_refund('cs1.abc', 'TLOCAL2026100807', '花蔫了'))
    assert r['ok'] is False and r['code'] == 'CHANNEL_UNAVAILABLE'
    assert r['action'] == 'apply_refund'


def test_refund_still_validates(monkeypatch):
    _write_env(monkeypatch)
    assert _run(ca.apply_refund('', 'T1')) == failure('AUTH_REQUIRED')
    assert _run(ca.apply_refund('cs1.x', 'bad id')) == failure('INVALID_ARGUMENT')


# ── 改地址：通道未打通 → 如实兜底 ──────────────────────────────

def test_update_order_address_channel_unavailable(monkeypatch):
    _write_env(monkeypatch)
    r = _run(ca.update_order_address('cs1.abc', 'TLOCAL2026100801', {'name': 'x', 'phone': '1'}))
    assert r['ok'] is False
    assert r['code'] == 'CHANNEL_UNAVAILABLE'
    assert r['action'] == 'update_order_address'


def test_update_order_address_still_validates(monkeypatch):
    _write_env(monkeypatch)
    r = _run(ca.update_order_address('cs1.abc', 'T; drop', {}))
    assert r == failure('INVALID_ARGUMENT')
    r2 = _run(ca.update_order_address('', 'T1', {}))
    assert r2 == failure('AUTH_REQUIRED')
