"""Timezone, atomic-claim, and lifecycle regression tests for reminders."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pytest

from backend import reminders

UTC = dt.timezone.utc
SHANGHAI = ZoneInfo('Asia/Shanghai')


def test_cron_uses_shanghai_even_with_utc_input():
    parsed = reminders.parse_cron('0 9 * * *')
    assert parsed is not None
    expected = dt.datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert reminders._next_after(parsed, dt.datetime(2030, 1, 1, 1, tzinfo=UTC)) == expected
    assert reminders._next_after(parsed, dt.datetime(2030, 1, 1, 9)) == expected
    assert reminders._next_after(parsed, dt.datetime(2030, 1, 1, 9, tzinfo=SHANGHAI)) == expected


@pytest.mark.parametrize('repeat,schedule,expected', [
    (False, '2099-03-04 09:30', '2099-03-04T01:30:00+00:00'),
    (True, '30 9 * * *', None),
])
def test_create_reminder_writes_aware_utc(monkeypatch, repeat, schedule, expected):
    monkeypatch.setattr(reminders.settings, 'CUSTOMER_SERVICE_ENABLED', True)
    params = []

    class Connection:
        def execute(self, sql, values):
            params.append(values)
            return self

        def fetchone(self):
            return {'id': 7}

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(reminders, 'transaction', transaction)
    result = asyncio.run(reminders.create_reminder('user', 'water flowers',
        repeat=repeat, cron=schedule if repeat else '',
        first_run='' if repeat else schedule))
    assert result['ok']
    next_run = params[0][5]
    assert next_run.tzinfo is not None and next_run.utcoffset() == dt.timedelta(0)
    assert result['data']['next_run'] == next_run.isoformat()
    if expected:
        assert next_run.isoformat() == expected
    else:
        assert next_run.astimezone(SHANGHAI).hour == 9
        assert next_run.astimezone(SHANGHAI).minute == 30


def test_fire_due_claims_rows_before_inserting(monkeypatch):
    statements = []
    due = {'id': 5, 'user_id': 'u', 'content': 'flowers', 'cron': '0 9 * * *', 'repeat': True}

    class Cursor:
        def fetchall(self):
            return [due]

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            return Cursor()

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(reminders, 'transaction', transaction)
    assert reminders._fire_due() == 1
    claim, insert, advance = statements
    assert 'FOR UPDATE SKIP LOCKED' in claim[0]
    assert 'ORDER BY next_run, id LIMIT 50' in claim[0]
    assert claim[1][0].utcoffset() == dt.timedelta(0)
    assert 'INSERT INTO notifications' in insert[0]
    assert 'UPDATE reminders SET next_run' in advance[0]
    assert advance[1][0].utcoffset() == dt.timedelta(0)
    assert advance[1][1] == due['id']


def test_stop_then_start_creates_new_worker(monkeypatch):
    # A short polling period exercises stop/start without waiting a minute.
    monkeypatch.setattr(reminders, '_SCHED_INTERVAL', 0.005)
    fired = []
    monkeypatch.setattr(reminders, '_fire_due', lambda: fired.append(1))
    reminders.stop_scheduler()
    try:
        reminders.start_scheduler()
        first = reminders._sched_thread
        reminders.start_scheduler()
        assert reminders._sched_thread is first
        reminders.stop_scheduler()
        assert first is not None and not first.is_alive()
        stopped_count = len(fired)
        reminders.start_scheduler()
        second = reminders._sched_thread
        assert second is not first and second is not None and second.is_alive()
        assert len(fired) >= stopped_count
    finally:
        reminders.stop_scheduler()
    assert second is not None and not second.is_alive()


@pytest.fixture
def postgres_reminders(monkeypatch):
    """Opt-in PostgreSQL integration fixture; never touches application tables."""
    url = os.environ.get('REMINDER_TEST_DATABASE_URL')
    if not url:
        pytest.skip('set REMINDER_TEST_DATABASE_URL for the PostgreSQL claim integration test')
    import psycopg
    from psycopg.rows import dict_row
    from backend.storage.db import ConnectionAdapter

    schema = 'test_reminders_' + uuid.uuid4().hex
    with psycopg.connect(url, row_factory=dict_row) as admin:
        admin.execute(f'CREATE SCHEMA {schema}')
        admin.execute(f'''CREATE TABLE {schema}.reminders (
            id BIGSERIAL PRIMARY KEY, user_id TEXT, content TEXT, cron TEXT,
            repeat BOOLEAN, next_run TIMESTAMPTZ, status TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW())''')
        admin.execute(f'''CREATE TABLE {schema}.notifications (
            id TEXT PRIMARY KEY, user_id TEXT, type TEXT, title TEXT, body TEXT)''')

    @contextmanager
    def transaction():
        with psycopg.connect(url, row_factory=dict_row) as raw:
            raw.execute(f'SET search_path TO {schema}')
            yield ConnectionAdapter(raw)

    monkeypatch.setattr(reminders, 'transaction', transaction)
    try:
        yield transaction
    finally:
        with psycopg.connect(url) as admin:
            admin.execute(f'DROP SCHEMA {schema} CASCADE')


def test_postgres_locked_reminder_is_not_double_fired(postgres_reminders):
    with postgres_reminders() as conn:
        conn.execute("INSERT INTO reminders (user_id, content, repeat, next_run, status) "
                     "VALUES ('user', 'order flowers', FALSE, ?, 'active')",
                     (dt.datetime(2020, 1, 1, tzinfo=UTC),))
    # Keep one transaction's row lock while another connection tries to claim.
    # A non-SKIP-LOCKED query would block here instead of immediately returning 0.
    with postgres_reminders() as locked:
        locked.execute('SELECT id FROM reminders FOR UPDATE').fetchall()
        assert reminders._fire_due() == 0
    assert reminders._fire_due() == 1
    assert reminders._fire_due() == 0
    with postgres_reminders() as conn:
        assert conn.execute('SELECT count(*) AS n FROM notifications').fetchone()['n'] == 1
        assert conn.execute('SELECT status FROM reminders').fetchone()['status'] == 'done'
