"""提醒/通知读取 API 回归测试。

安全重点：user_id 一律来自 token（Depends(current_user)），路由层绝不
信任请求参数里的身份；属主过滤内建在 WHERE 条件——读不到、也改不掉
别人的提醒/通知。DB 走隔离测试库（flora_agent_test）。

异步惯例：与全库一致用 asyncio.run（无 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.storage import db as mc  # noqa: E402


@pytest.fixture()
def test_db(monkeypatch):
    """注入测试库 + 真库建表（幂等）；每例后清空提醒/通知表。

    与 test_merchant_contact.py 同一模式：settings 是模块级单例，
    必须用 monkeypatch 注入（env setdefault 在先跑的测试后无效）。
    """
    from backend.storage import db
    conn = getattr(db._thread_local, 'conn', None)
    if conn is not None:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        db._thread_local.conn = None
    monkeypatch.setattr(mc.settings, 'DATABASE_URL',
                        'postgresql://postgres@127.0.0.1:5433/flora_agent_test')
    db.init_db()
    with db.transaction() as c:
        c.execute('DELETE FROM reminders')
        c.execute('DELETE FROM notifications')
    yield c
    with db.transaction() as c:
        c.execute('DELETE FROM reminders')
        c.execute('DELETE FROM notifications')
    db._thread_local.conn = None


def _row(r) -> dict:
    return dict(r) if hasattr(r, 'keys') else dict(r)


def _insert_reminder(conn, user_id: str, content: str, **kw) -> int:
    cur = conn.execute(
        'INSERT INTO reminders (user_id, content, cron, first_run, repeat, next_run, status) '
        'VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id',
        (user_id, content, kw.get('cron'), kw.get('first_run'), kw.get('repeat', False),
         kw.get('next_run', _dt.datetime.now() + _dt.timedelta(days=1)), kw.get('status', 'active')))
    return int(_row(cur.fetchone())['id'])


def _insert_notification(conn, user_id: str, title: str) -> str:
    nid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO notifications (id, user_id, type, title, body, push_channel) "
        "VALUES (?, ?, 'reminder', ?, ?, 'inbox')", (nid, user_id, title, title + ' 内容'))
    return nid


def test_list_notifications_owner_only(test_db):
    from backend.routers import notifications as napi
    conn = test_db
    _insert_notification(conn, 'u_owner', '我的提醒')
    _insert_notification(conn, 'u_other', '别人的提醒')
    r = asyncio.run(napi.list_notifications(user_id='u_owner'))
    assert r['ok'] is True
    titles = [i['title'] for i in r['data']['items']]
    assert '我的提醒' in titles and '别人的提醒' not in titles
    assert r['data']['unread'] >= 1


def test_mark_read_owner_only(test_db):
    from backend.routers import notifications as napi
    conn = test_db
    nid = _insert_notification(conn, 'u_owner', '待读')
    r = asyncio.run(napi.mark_read(nid, user_id='u_owner'))
    assert r['ok'] is True and r['data']['updated'] == 1
    nid2 = _insert_notification(conn, 'u_other', '他人的')
    r2 = asyncio.run(napi.mark_read(nid2, user_id='u_owner'))
    assert r2['ok'] is True and r2['data']['updated'] == 0


def test_reminders_crud_owner_only(test_db):
    from backend.routers import notifications as napi
    conn = test_db
    rid = _insert_reminder(conn, 'u_owner', '每月8号给妈订花', cron='0 9 8 * *', repeat=True)
    _insert_reminder(conn, 'u_other', '他人提醒', first_run='2026-12-24 18:00')
    r = asyncio.run(napi.list_reminders(user_id='u_owner'))
    contents = [i['content'] for i in r['data']['items']]
    assert '每月8号给妈订花' in contents and '他人提醒' not in contents
    assert any(i['repeat'] and i['cron'] == '0 9 8 * *' for i in r['data']['items'])
    d = asyncio.run(napi.delete_reminder(rid, user_id='u_owner'))
    assert d['ok'] is True and d['data']['deleted'] == 1
    d2 = asyncio.run(napi.delete_reminder(rid, user_id='u_owner'))
    assert d2['ok'] is False and d2['code'] == 'NOT_FOUND'
    rows = conn.execute("SELECT id FROM reminders WHERE user_id='u_other'").fetchall()
    rid_other = int(_row(rows[0])['id'])
    d3 = asyncio.run(napi.delete_reminder(rid_other, user_id='u_owner'))
    assert d3['ok'] is False and d3['code'] == 'NOT_FOUND'


def test_notification_pagination_bounds(test_db):
    from backend.routers import notifications as napi
    r = asyncio.run(napi.list_notifications(user_id='nobody_x', limit=500, offset=-5))
    assert r['ok'] is True and r['data']['items'] == []
