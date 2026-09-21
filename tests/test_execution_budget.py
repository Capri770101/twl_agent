import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend import execution
from agent.engine import llm


def test_cancelled_request_cannot_start_transaction(monkeypatch):
    from backend.storage import db
    conn = Mock()
    monkeypatch.setattr(db, 'get_conn', lambda: conn)
    state = execution.Execution('u', time.monotonic() + 60)
    token = execution.current.set(state)
    try:
        state.cancelled.set()
        with pytest.raises(TimeoutError), db.transaction():
            pytest.fail('cancelled transaction entered')
        conn.execute.assert_not_called()
    finally:
        execution.current.reset(token)


def test_cancel_before_commit_rolls_back(monkeypatch):
    from backend.storage import db
    conn = Mock()
    monkeypatch.setattr(db, 'get_conn', lambda: conn)
    state = execution.Execution('u', time.monotonic() + 60)
    token = execution.current.set(state)
    try:
        with pytest.raises(TimeoutError):
            with db.transaction():
                state.cancelled.set()
        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
    finally:
        execution.current.reset(token)


def test_worker_copies_call_identity_and_signals_cancel():
    from backend import observability
    import threading
    entered, release = threading.Event(), threading.Event()
    captured = []

    async def work():
        captured.append((execution.current.get(), observability.get_call_context()))
        entered.set()
        release.wait(2)

    async def scenario(pool):
        observability.set_call_context(321)
        task = asyncio.create_task(execution.run_worker(pool, work, 'u', 60))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert captured[0][0].cancelled.is_set()
        assert captured[0][0].user_id == 'u'
        assert captured[0][1] == 321
        release.set()
        observability.set_call_context(None)

    with ThreadPoolExecutor(max_workers=1) as pool:
        asyncio.run(scenario(pool))


def test_stream_usage_inherits_user_and_records_once(monkeypatch):
    class Stream:
        closed = False
        def __iter__(self):
            yield SimpleNamespace(choices=[1], usage=None)
            yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4))
        def close(self):
            self.closed = True
    stream = Stream()
    record = Mock()
    monkeypatch.setattr(llm, '_llm_configured', lambda: True)
    monkeypatch.setattr(llm.budget, 'check', lambda uid: (True, None))
    monkeypatch.setattr(llm, '_try_providers', lambda *args: stream)
    monkeypatch.setattr(llm, '_record_cost', record)
    token = execution.current.set(execution.Execution('user-x', time.monotonic() + 60))
    try:
        assert len(list(llm.call_llm_stream([]))) == 2
        assert record.call_count == 1
        assert record.call_args.args[0] == 'user-x'
        assert stream.closed
    finally:
        execution.current.reset(token)


def test_retry_uses_remaining_total_budget(monkeypatch):
    clock = [100.0]
    seen = []
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(llm, '_providers', lambda: [{'name': 'fake'}])
    monkeypatch.setattr(llm, '_cb', lambda name: Mock(allow=lambda: True))
    monkeypatch.setattr(llm, '_is_retryable', lambda exc: True)
    monkeypatch.setattr(llm, '_backoff', lambda attempt: None)
    monkeypatch.setattr(type(llm.settings), 'llm_retry_max_attempts', property(lambda self: 3))

    def raw(*args):
        seen.append(args[-1])
        clock[0] += 6
        raise RuntimeError('transient')

    monkeypatch.setattr(llm, '_raw_call', raw)
    with pytest.raises(TimeoutError):
        llm._try_providers([], None, False, None, 'u', timeout=10)
    assert seen == [10, 4]
