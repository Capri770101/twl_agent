"""A concurrent claim of a user-provided session ID must not bypass ownership."""
import asyncio
from contextlib import contextmanager

import pytest

from backend.storage import memory


@pytest.mark.parametrize(('competing_owner', 'allowed'), [('other', False), ('owner', True)])
def test_conflicting_session_insert_rechecks_owner(monkeypatch, competing_owner, allowed):
    queries = []

    class Cursor:
        def __init__(self, result):
            self.result = result
        def fetchone(self):
            return self.result

    class Connection:
        def execute(self, sql, args):
            queries.append((sql, args))
            if 'DO NOTHING RETURNING session_id' in sql:
                return Cursor(None)  # another request inserted this ID first
            if len(queries) == 1:
                return Cursor(None)  # no row visible before the insert race
            return Cursor({'user_id': competing_owner})

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(memory, 'transaction', transaction)
    if allowed:
        assert asyncio.run(memory.get_or_create_session('owner', 'chosen-id')) == 'chosen-id'
    else:
        with pytest.raises(PermissionError, match='conversation does not belong'):
            asyncio.run(memory.get_or_create_session('owner', 'chosen-id'))
    assert len(queries) == 3
    assert queries[-1][1] == ('chosen-id',)


def test_new_session_insert_returning_stays_owned(monkeypatch):
    queries = []
    class Cursor:
        def __init__(self, row):
            self.row = row
        def fetchone(self):
            return self.row
    class Connection:
        def execute(self, sql, args):
            queries.append(sql)
            return Cursor({'session_id': 'chosen-id'} if 'RETURNING session_id' in sql else None)
    @contextmanager
    def transaction():
        yield Connection()
    monkeypatch.setattr(memory, 'transaction', transaction)
    assert asyncio.run(memory.get_or_create_session('owner', 'chosen-id')) == 'chosen-id'
    assert len(queries) == 2
