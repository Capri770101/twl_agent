"""Manual PostgreSQL retention; preview by default, --apply explicitly deletes."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json

# Identifiers/predicates are fixed; externally supplied values are bound parameters.
POLICY = {
    'merchant_tickets': ("status = 'closed' AND updated_at < %s", True),
    # Actual runtime completed state is 'done', not 'completed'.
    'reminders': ("status IN ('cancelled', 'done') AND updated_at < %s", True),
    'notifications': ('is_read = 1 AND created_at < %s', True),
    'messages': ('session_id IN (SELECT session_id FROM sessions WHERE updated_at < %s{owner})', False),
    'sessions': ('updated_at < %s', True),
    'tool_call_logs': ('(created_at < %s OR call_log_id IN (SELECT id FROM call_logs WHERE created_at < %s))', False),
    'call_logs': ('created_at < %s', True),
    'speech_logs': ('created_at < %s', True),
    'customer_service_logs': ('created_at < %s', False),
}


def _predicate(table: str, cutoff: datetime, user_id: str | None) -> tuple[str, tuple]:
    condition, owned = POLICY[table]
    args: tuple = (cutoff, cutoff) if table == 'tool_call_logs' else (cutoff,)
    if table == 'messages':
        condition = condition.format(owner=' AND user_id = %s' if user_id is not None else '')
        if user_id is not None:
            args += (user_id,)
    elif user_id is not None and table == 'tool_call_logs':
        condition = f'({condition}) AND call_log_id IN (SELECT id FROM call_logs WHERE user_id = %s)'
        args += (user_id,)
    elif owned and user_id is not None:
        condition += ' AND user_id = %s'
        args += (user_id,)
    return condition, args


def cleanup(conn, *, days: int = 90, apply: bool = False,
            user_id: str | None = None, now: datetime | None = None) -> dict:
    """Use one idle psycopg connection transaction; errors roll back all deletes."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError('days must be a positive integer')
    if user_id is not None and (not isinstance(user_id, str) or not user_id.strip()):
        raise ValueError('user_id must be a nonempty string')
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('now must be timezone-aware')
    cutoff = instant - timedelta(days=days)
    counts = {}
    with conn.transaction():
        # In apply mode lock all affected tables to prevent concurrent session
        # activity/state changes between eligibility selection and child deletion.
        if apply:
            conn.execute('LOCK TABLE ' + ', '.join(POLICY) + ' IN SHARE ROW EXCLUSIVE MODE')
        for table in POLICY:
            if user_id is not None and table == 'customer_service_logs':
                counts[table] = 0
                continue
            predicate, params = _predicate(table, cutoff, user_id)
            if apply:
                # sessions uses session_id as its primary key.
                key = 'session_id' if table == 'sessions' else 'id'
                cursor = conn.execute(f'DELETE FROM {table} WHERE {predicate} RETURNING {key}', params)
                counts[table] = cursor.rowcount
            else:
                cursor = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {predicate}', params)
                counts[table] = cursor.fetchone()[0]
    return {'mode': 'applied' if apply else 'dry-run', 'days': days,
            'cutoff_utc': cutoff.astimezone(timezone.utc).isoformat(),
            'user_id': user_id, 'counts': counts, 'total': sum(counts.values()),
            'policy': {table: predicate.replace('{owner}', '') for table, (predicate, _) in POLICY.items()},
            'note': ('User-scoped run excludes ownerless customer_service_logs.'
                     if user_id is not None else None)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Preview or explicitly apply application PostgreSQL retention')
    parser.add_argument('--days', type=int, default=90, help='positive retention days (default: 90)')
    parser.add_argument('--apply', action='store_true', help='commit eligible deletions; otherwise preview')
    parser.add_argument('--user-id', help='optional exact owner filter; skips ownerless service logs')
    args = parser.parse_args(argv)
    if args.days <= 0:
        parser.error('--days must be positive')
    from backend.storage.db import _database_url
    import psycopg
    with psycopg.connect(_database_url(), connect_timeout=10) as conn:
        report = cleanup(conn, days=args.days, apply=args.apply, user_id=args.user_id)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
