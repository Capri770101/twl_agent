"""Chat-route storage calls must not block the loop or move a transaction across threads."""
import asyncio
import threading
import time
from contextlib import contextmanager

import pytest

from backend.execution import Execution, checkpoint, current
from backend.storage import memory


@pytest.mark.parametrize('operation', [
    'create_conversation', 'get_conversation', 'update_conversation_preview',
    'list_conversations', 'delete_conversation', 'load_history',
])
def test_chat_transaction_stays_in_worker_and_loop_keeps_heartbeat(monkeypatch, operation):
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Cursor:
        def fetchone(self):
            if operation == 'get_conversation':
                return {'session_id': 's', 'user_id': 'u', 'title': 'title'}
            if operation == 'delete_conversation':
                return {'session_id': 's'}
            return None

        def fetchall(self):
            if operation == 'list_conversations':
                return [{'session_id': 's', 'title': 'title', 'preview': None,
                         'shop_id': None, 'created_at': 'before', 'updated_at': 'now'}]
            if operation == 'load_history':
                return [{'role': 'assistant', 'content': 'hello', 'ui': None, 'data': '{"x": 1}'}]
            return []

    class Connection:
        def execute(self, sql, args):
            calls.append(('execute', threading.get_ident(), sql))
            return Cursor()

    @contextmanager
    def transaction():
        thread = threading.get_ident()
        calls.append(('enter', thread, None))
        checkpoint()  # to_thread must copy the request context into the worker.
        entered.set()
        assert release.wait(timeout=3), 'test did not release the blocked database operation'
        try:
            yield Connection()
            checkpoint()
        finally:
            calls.append(('exit', threading.get_ident(), None))

    monkeypatch.setattr(memory, 'transaction', transaction)

    async def scenario():
        if operation == 'create_conversation':
            call = memory.create_conversation('u')
        elif operation == 'get_conversation':
            call = memory.get_conversation('s')
        elif operation == 'update_conversation_preview':
            call = memory.update_conversation_preview('s', 'hello')
        elif operation == 'list_conversations':
            call = memory.list_conversations('u')
        elif operation == 'delete_conversation':
            call = memory.delete_conversation('s', 'u')
        else:
            call = memory.load_history('s', 5)
        task = asyncio.create_task(call)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # A synchronous transaction on the event-loop thread would prevent this heartbeat.
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        finally:
            release.set()
        return await asyncio.wait_for(task, timeout=2)

    result = asyncio.run(scenario())
    assert calls[0][0] == 'enter'
    assert calls[-1][0] == 'exit'
    assert all(thread == calls[0][1] != loop_thread for _, thread, _ in calls)
    if operation == 'get_conversation':
        assert result['user_id'] == 'u'
    elif operation == 'list_conversations':
        assert result[0]['id'] == 's'
    elif operation == 'load_history':
        assert result == [{'role': 'assistant', 'content': 'hello', 'data': {'x': 1}}]
    elif operation == 'delete_conversation':
        assert result is True
        assert 'FOR UPDATE' in calls[1][2]
        assert len([call for call in calls if call[0] == 'execute']) == 3


def test_cancelled_request_context_reaches_transaction_checkpoint(monkeypatch):
    observed = []

    @contextmanager
    def transaction():
        observed.append(current.get())
        checkpoint()
        raise AssertionError('cancelled request entered transaction')
        yield  # pragma: no cover

    monkeypatch.setattr(memory, 'transaction', transaction)
    state = Execution('u', time.monotonic() + 60)
    state.cancelled.set()

    async def scenario():
        token = current.set(state)
        try:
            with pytest.raises(TimeoutError, match='cancelled'):
                await memory.update_conversation_preview('s', 'value')
        finally:
            current.reset(token)

    asyncio.run(scenario())
    assert observed == [state]


def test_delete_wrong_owner_never_deletes_messages(monkeypatch):
    queries = []

    class Cursor:
        def fetchone(self):
            return None

    class Connection:
        def execute(self, sql, args):
            queries.append((sql, args))
            return Cursor()

    @contextmanager
    def transaction():
        yield Connection()

    monkeypatch.setattr(memory, 'transaction', transaction)
    assert asyncio.run(memory.delete_conversation('s', 'other')) is False
    assert len(queries) == 1
    assert queries[0][1] == ('s', 'other')
