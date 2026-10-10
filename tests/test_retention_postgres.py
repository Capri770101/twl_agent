"""Optional PostgreSQL integration checks for retention, isolated in a disposable schema."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.retention_cleanup import cleanup


@pytest.mark.skipif(not os.getenv('FLORA_TEST_DATABASE_URL'), reason='isolated test PostgreSQL opt-in')
def test_retention_sql_protects_active_and_other_users():
    import psycopg

    schema = 'retention_' + uuid.uuid4().hex
    url = os.environ['FLORA_TEST_DATABASE_URL']
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=100)
    with psycopg.connect(url) as admin:
        admin.execute(f'CREATE SCHEMA {schema}')
        admin.commit()
        try:
            with psycopg.connect(url, options=f'-c search_path={schema}') as conn:
                conn.execute('CREATE TABLE merchant_tickets (id bigint, user_id text, status text, updated_at timestamptz)')
                conn.execute('CREATE TABLE reminders (id bigint, user_id text, status text, updated_at timestamptz)')
                conn.execute('CREATE TABLE notifications (id bigint, user_id text, is_read int, created_at timestamptz)')
                conn.execute('CREATE TABLE sessions (session_id text, user_id text, updated_at timestamptz)')
                conn.execute('CREATE TABLE messages (id bigint, session_id text)')
                conn.execute('CREATE TABLE call_logs (id bigint, user_id text, created_at timestamptz)')
                conn.execute('CREATE TABLE tool_call_logs (id bigint, call_log_id bigint, created_at timestamptz)')
                conn.execute('CREATE TABLE speech_logs (id bigint, user_id text, created_at timestamptz)')
                conn.execute('CREATE TABLE customer_service_logs (id bigint, created_at timestamptz)')
                conn.execute('INSERT INTO call_logs VALUES (1, %s, %s), (2, %s, %s)',
                             ('owner', cutoff_time, 'other', cutoff_time))
                conn.execute('INSERT INTO tool_call_logs VALUES (1, 1, %s), (2, 2, %s)',
                             (cutoff_time, cutoff_time))
                conn.execute('INSERT INTO merchant_tickets VALUES (1, %s, %s, %s), (2, %s, %s, %s)',
                             ('owner', 'closed', cutoff_time, 'owner', 'open', cutoff_time))
                conn.execute('INSERT INTO reminders VALUES (1, %s, %s, %s), (2, %s, %s, %s)',
                             ('owner', 'done', cutoff_time, 'owner', 'active', cutoff_time))
                conn.execute('INSERT INTO notifications VALUES (1, %s, 1, %s), (2, %s, 0, %s)',
                             ('owner', cutoff_time, 'owner', cutoff_time))
                conn.commit()
                preview = cleanup(conn, user_id='owner')
                assert preview['mode'] == 'dry-run'
                assert conn.execute('SELECT COUNT(*) FROM tool_call_logs').fetchone()[0] == 2
                result = cleanup(conn, user_id='owner', apply=True)
                assert result['counts']['tool_call_logs'] == 1
                assert conn.execute('SELECT id FROM tool_call_logs').fetchall() == [(2,)]
                assert conn.execute('SELECT id FROM call_logs').fetchall() == [(2,)]
                assert conn.execute('SELECT status FROM merchant_tickets').fetchall() == [('open',)]
                assert conn.execute('SELECT status FROM reminders').fetchall() == [('active',)]
                assert conn.execute('SELECT is_read FROM notifications').fetchall() == [(0,)]
        finally:
            admin.execute(f'DROP SCHEMA {schema} CASCADE')
