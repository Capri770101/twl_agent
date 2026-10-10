"""Retention safety tests without a live PostgreSQL database."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import pytest
from scripts.retention_cleanup import POLICY, cleanup

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


class Cursor:
    rowcount = 2

    def fetchone(self):
        return (2,)


class Connection:
    def __init__(self, fail_on=None):
        self.queries = []
        self.committed = False
        self.rolled_back = False
        self.fail_on = fail_on

    @contextmanager
    def transaction(self):
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if self.fail_on and self.fail_on in query:
            raise RuntimeError('simulated SQL failure')
        return Cursor()


def test_default_dry_run_never_deletes_and_reports_each_table():
    conn = Connection()
    result = cleanup(conn, now=NOW)
    assert result['mode'] == 'dry-run' and result['days'] == 90
    assert result['cutoff_utc'] == (NOW - timedelta(days=90)).isoformat()
    assert set(result['counts']) == set(POLICY)
    assert result['total'] == len(POLICY) * 2
    assert all(sql.startswith('SELECT COUNT(*)') for sql, _ in conn.queries)
    assert all(params[0] == NOW - timedelta(days=90) for _, params in conn.queries)
    assert conn.committed and not conn.rolled_back


def test_apply_deletes_children_before_parents_inside_transaction():
    conn = Connection()
    result = cleanup(conn, now=NOW, apply=True)
    sql = [q for q, _ in conn.queries]
    assert sql[0].startswith('LOCK TABLE ')
    assert all(q.startswith('DELETE FROM ') for q in sql[1:])
    assert sql.index(next(q for q in sql if q.startswith('DELETE FROM messages '))) < sql.index(
        next(q for q in sql if q.startswith('DELETE FROM sessions ')))
    assert sql.index(next(q for q in sql if q.startswith('DELETE FROM tool_call_logs '))) < sql.index(
        next(q for q in sql if q.startswith('DELETE FROM call_logs ')))
    assert result['total'] == len(POLICY) * 2 and result['mode'] == 'applied'
    assert conn.committed


def test_owner_bound_and_no_cross_user_child_or_ownerless_service_logs():
    conn = Connection()
    hostile = "a' OR TRUE --"
    report = cleanup(conn, now=NOW, user_id=hostile, apply=True)
    assert report['counts']['customer_service_logs'] == 0
    queries = [(q, p) for q, p in conn.queries if q.startswith('DELETE')]
    assert len(queries) == len(POLICY) - 1
    assert all(hostile not in q and hostile in p for q, p in queries)
    messages = next(q for q, _ in queries if q.startswith('DELETE FROM messages'))
    tools = next(q for q, _ in queries if q.startswith('DELETE FROM tool_call_logs'))
    assert 'sessions WHERE updated_at < %s AND user_id = %s' in messages
    assert 'call_logs WHERE user_id = %s' in tools
    assert "WHERE ((created_at < %s OR call_log_id IN (SELECT id FROM call_logs WHERE created_at < %s))) AND call_log_id IN" in tools


def test_protected_states_and_expiry_are_exclusive():
    conn = Connection()
    cleanup(conn, now=NOW, apply=True)
    ticket = next(q for q, _ in conn.queries if q.startswith('DELETE FROM merchant_tickets'))
    reminder = next(q for q, _ in conn.queries if q.startswith('DELETE FROM reminders'))
    notification = next(q for q, _ in conn.queries if q.startswith('DELETE FROM notifications'))
    session = next(q for q, _ in conn.queries if q.startswith('DELETE FROM sessions'))
    assert "status = 'closed' AND updated_at < %s" in ticket
    assert "status IN ('cancelled', 'done') AND updated_at < %s" in reminder
    assert "'active'" not in reminder and "'paused'" not in reminder
    assert 'is_read = 1 AND created_at < %s' in notification
    assert 'updated_at < %s' in session and 'created_at < %s' not in session


def test_failure_rolls_back_all_work():
    conn = Connection(fail_on='DELETE FROM sessions ')
    with pytest.raises(RuntimeError, match='simulated'):
        cleanup(conn, now=NOW, apply=True)
    assert conn.rolled_back and not conn.committed


@pytest.mark.parametrize('days', [0, -1, True, 1.5])
def test_invalid_days_fail_closed(days):
    conn = Connection()
    with pytest.raises(ValueError):
        cleanup(conn, days=days)
    assert not conn.queries
