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




def _future_shanghai(days: int = 2) -> str:
    from zoneinfo import ZoneInfo
    return (_dt.datetime.now(ZoneInfo('Asia/Shanghai')) +
            _dt.timedelta(days=days)).strftime('%Y-%m-%d %H:%M')


def test_reminder_pause_resume_and_due_processing(test_db):
    from backend.routers import notifications as napi
    from backend.reminders import _fire_due
    conn = test_db
    rid = _insert_reminder(conn, 'u_owner', '原内容', cron='0 9 * * *', repeat=True,
                           next_run=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1))
    paused = asyncio.run(napi.pause_reminder(rid, user_id='u_owner'))
    assert paused['ok'] and paused['data']['status'] == 'paused'
    assert paused['data']['timezone'] == 'Asia/Shanghai'
    assert _fire_due() == 0
    assert asyncio.run(napi.pause_reminder(rid, user_id='u_owner'))['code'] == 'NOT_FOUND'
    resumed = asyncio.run(napi.resume_reminder(rid, user_id='u_owner'))
    assert resumed['ok'] and resumed['data']['status'] == 'active'
    assert _dt.datetime.fromisoformat(resumed['data']['next_run']) > _dt.datetime.now(_dt.timezone.utc)
    assert _fire_due() == 0  # no catch-up delivery on resume
    assert conn.execute('SELECT count(*) AS n FROM notifications WHERE user_id=?',
                        ('u_owner',)).fetchone()['n'] == 0


def test_reminder_modify_content_and_schedule_owner_only(test_db):
    from backend.routers import notifications as napi
    conn = test_db
    rid = _insert_reminder(conn, 'u_owner', '原内容', first_run=_future_shanghai())
    other = _insert_reminder(conn, 'u_other', '他人内容')
    Patch = napi.ReminderPatch
    assert asyncio.run(napi.modify_reminder(other, Patch(content='越权'), user_id='u_owner'))['code'] == 'NOT_FOUND'
    assert asyncio.run(napi.pause_reminder(other, user_id='u_owner'))['code'] == 'NOT_FOUND'
    assert asyncio.run(napi.resume_reminder(other, user_id='u_owner'))['code'] == 'NOT_FOUND'
    assert conn.execute('SELECT content FROM reminders WHERE id=?', (other,)).fetchone()['content'] == '他人内容'
    content = asyncio.run(napi.modify_reminder(rid, Patch(content=' 新内容 '), user_id='u_owner'))
    assert content['data']['content'] == '新内容' and not content['data']['repeat']
    asyncio.run(napi.pause_reminder(rid, user_id='u_owner'))
    changed = asyncio.run(napi.modify_reminder(
        rid, Patch(cron='0 9 * * *'), user_id='u_owner'))
    assert changed['ok'] and changed['data']['status'] == 'paused'
    assert changed['data']['repeat'] and changed['data']['first_run'] is None
    assert changed['data']['next_run_local'].endswith('09:00')
    assert asyncio.run(napi.resume_reminder(rid, user_id='u_owner'))['ok']
    once = asyncio.run(napi.modify_reminder(
        rid, Patch(first_run=_future_shanghai(3)), user_id='u_owner'))
    assert once['ok'] and not once['data']['repeat'] and once['data']['cron'] is None
    assert once['data']['next_run_local'] == once['data']['first_run']
    listed = asyncio.run(napi.list_reminders(user_id='u_owner'))['data']['items']
    assert listed[0]['next_run_local'] == once['data']['first_run']


def test_reminder_invalid_edits_and_expired_oneoff_resume(test_db):
    from backend.routers import notifications as napi
    conn = test_db
    rid = _insert_reminder(conn, 'u_owner', '原内容', first_run=_future_shanghai())
    Patch = napi.ReminderPatch
    for patch in (Patch(), Patch(content='  '), Patch(content='x' * 201),
                  Patch(first_run='2000-01-01 12:00'), Patch(first_run='2026-02-30 12:00'),
                  Patch(cron='bad cron'), Patch(cron='0 9 * * *', first_run=_future_shanghai())):
        assert asyncio.run(napi.modify_reminder(rid, patch, user_id='u_owner'))['code'] == 'INVALID_ARGUMENT'
    assert conn.execute('SELECT content FROM reminders WHERE id=?', (rid,)).fetchone()['content'] == '原内容'
    asyncio.run(napi.pause_reminder(rid, user_id='u_owner'))
    conn.execute('UPDATE reminders SET first_run=?, next_run=? WHERE id=?',
                 ('2000-01-01 12:00', _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc), rid))
    assert asyncio.run(napi.resume_reminder(rid, user_id='u_owner'))['code'] == 'INVALID_ARGUMENT'
    assert asyncio.run(napi.modify_reminder(
        rid, Patch(first_run=_future_shanghai()), user_id='u_owner'))['ok']
    assert asyncio.run(napi.resume_reminder(rid, user_id='u_owner'))['ok']
    asyncio.run(napi.delete_reminder(rid, user_id='u_owner'))
    assert asyncio.run(napi.modify_reminder(rid, Patch(content='no'), user_id='u_owner'))['code'] == 'NOT_FOUND'
    assert asyncio.run(napi.resume_reminder(rid, user_id='u_owner'))['code'] == 'NOT_FOUND'


def test_conversation_can_manage_only_own_reminders(test_db):
    import json
    from agent.customer_tools import manage_my_reminders
    conn = test_db
    mine = _insert_reminder(conn, 'owner', '母亲生日', first_run=_future_shanghai())
    other = _insert_reminder(conn, 'other', '秘密提醒', first_run=_future_shanghai())
    async def call(action, **kw):
        return json.loads(await manage_my_reminders(action, _context={'user_id': 'owner'}, **kw))
    listed = asyncio.run(call('list'))
    assert [x['id'] for x in listed['data']['items']] == [mine]
    assert asyncio.run(call('pause', reminder_id=other))['code'] == 'NOT_FOUND'
    assert asyncio.run(call('pause', reminder_id=mine))['data']['status'] == 'paused'
    changed = asyncio.run(call('update', reminder_id=mine, first_run=_future_shanghai(3)))
    assert changed['data']['next_run_local'] == changed['data']['first_run']
    assert asyncio.run(call('resume', reminder_id=mine))['data']['status'] == 'active'
    assert asyncio.run(call('delete', reminder_id=mine))['data']['deleted'] == 1
    assert asyncio.run(call('resume', reminder_id=mine))['code'] == 'NOT_FOUND'
    assert asyncio.run(call('delete'))['code'] == 'INVALID_ARGUMENT'
    assert json.loads(asyncio.run(manage_my_reminders('list', _context={})))['code'] == 'AUTH_REQUIRED'


def test_notification_pagination_bounds(test_db):
    from backend.routers import notifications as napi
    r = asyncio.run(napi.list_notifications(user_id='nobody_x', limit=500, offset=-5))
    assert r['ok'] is True and r['data']['items'] == []
