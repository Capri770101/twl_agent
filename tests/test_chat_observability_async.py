"""The chat observability hooks must not stall the request event loop."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.routers import chat


def test_chat_observability_runs_in_workers_with_live_loop(monkeypatch):
    main_thread = threading.get_ident()
    calls = []
    next_cid = iter((1234, 2345, 3456, 4567))

    def hook(name, *args, **kwargs):
        assert threading.get_ident() != main_thread, name
        # Each synchronous hook waits for an event-loop heartbeat; executing even
        # one of them on the loop would prevent its own heartbeat from arriving.
        heartbeat = threading.Event()
        loop.call_soon_threadsafe(heartbeat.set)
        assert heartbeat.wait(1), f'{name} blocked the event loop'
        calls.append((name, args, kwargs))
        return next(next_cid) if name == 'start' else None

    monkeypatch.setattr(chat, 'record_call_start', lambda *a, **kw: hook('start', *a, **kw))
    monkeypatch.setattr(chat, 'record_call_end', lambda *a, **kw: hook('end', *a, **kw))
    monkeypatch.setattr(chat, 'record_tool_call', lambda *a, **kw: hook('tool', *a, **kw))
    monkeypatch.setattr(chat, '_enforce_rate_limit', lambda *a: None)
    monkeypatch.setattr(chat, '_spawn_consolidate', lambda *a: None)
    monkeypatch.setattr(chat, '_AGENT_SEM', asyncio.Semaphore(1))
    monkeypatch.setattr(chat.mem_store, 'get_conversation', AsyncMock(return_value={'user_id': 'u'}))
    monkeypatch.setattr(chat.mem_store, 'update_conversation_preview', AsyncMock())

    class Agent:
        async def arun(self, *args, **kwargs):
            return SimpleNamespace(session_id='s', tool_calls=[SimpleNamespace(name='search', status='success')],
                                   model_dump=lambda: {'ok': True})

        async def arun_stream(self, *args, **kwargs):
            yield {'event': 'done'}

    monkeypatch.setattr(chat, 'get_agent', Agent)

    async def scenario():
        nonlocal loop
        loop = asyncio.get_running_loop()
        req = chat.ChatRequest(message='hello', user_id='u', session_id='s')
        request = SimpleNamespace(headers={})
        assert await chat.chat(req, request, authenticated_user='u', user_info=None) == {'ok': True}
        response = await chat.chat_stream(req, request, authenticated_user='u', user_info=None)
        assert any(chunk.startswith('event: done') for chunk in [chunk async for chunk in response.body_iterator])
        # Busy paths also write their error metric from a worker.
        await chat._AGENT_SEM.acquire()
        monkeypatch.setattr(chat, '_BUSY_WAIT', 0.001)
        try:
            with pytest.raises(HTTPException) as error:
                await chat.chat(req, request, authenticated_user='u', user_info=None)
            assert error.value.status_code == 503
            response = await chat.chat_stream(req, request, authenticated_user='u', user_info=None)
            assert [chunk async for chunk in response.body_iterator][0].startswith('event: error')
        finally:
            chat._AGENT_SEM.release()

    loop = None
    asyncio.run(scenario())
    assert [name for name, _, _ in calls] == ['start', 'end', 'tool', 'start', 'end', 'start', 'end', 'start', 'end']
    assert [kwargs['cid'] for name, args, kwargs in calls if name != 'start'] == [1234, 1234, 2345, 3456, 4567]
    assert calls[1][1][0] == 'success'
    assert calls[6][1][0] == 'error'
    assert calls[8][1][0] == 'error'


def test_failed_call_start_does_not_reuse_worker_context(monkeypatch):
    """A worker may retain a prior cid, so never call end/tool with cid=None."""
    monkeypatch.setattr(chat, '_enforce_rate_limit', lambda *args: None)
    monkeypatch.setattr(chat, '_spawn_consolidate', lambda *args: None)
    monkeypatch.setattr(chat, '_AGENT_SEM', asyncio.Semaphore(1))
    monkeypatch.setattr(chat.mem_store, 'get_conversation', AsyncMock(return_value={'user_id': 'u'}))
    monkeypatch.setattr(chat.mem_store, 'update_conversation_preview', AsyncMock())
    monkeypatch.setattr(chat, 'record_call_start', lambda *args: None)

    def unexpected(*args, **kwargs):
        pytest.fail('a failed start must not invoke end/tool with a missing cid')

    monkeypatch.setattr(chat, 'record_call_end', unexpected)
    monkeypatch.setattr(chat, 'record_tool_call', unexpected)

    class Agent:
        async def arun(self, *args, **kwargs):
            return SimpleNamespace(session_id='s', tool_calls=[SimpleNamespace(name='search', status='success')],
                                   model_dump=lambda: {'ok': True})

        async def arun_stream(self, *args, **kwargs):
            yield {'event': 'done'}

    monkeypatch.setattr(chat, 'get_agent', Agent)

    async def scenario():
        req = chat.ChatRequest(message='hello', user_id='u', session_id='s')
        request = SimpleNamespace(headers={})
        assert await chat.chat(req, request, authenticated_user='u', user_info=None) == {'ok': True}
        response = await chat.chat_stream(req, request, authenticated_user='u', user_info=None)
        assert [chunk async for chunk in response.body_iterator][0].startswith('event: done')
        await chat._AGENT_SEM.acquire()
        monkeypatch.setattr(chat, '_BUSY_WAIT', 0.001)
        try:
            with pytest.raises(HTTPException) as error:
                await chat.chat(req, request, authenticated_user='u', user_info=None)
            assert error.value.status_code == 503
            response = await chat.chat_stream(req, request, authenticated_user='u', user_info=None)
            assert [chunk async for chunk in response.body_iterator][0].startswith('event: error')
        finally:
            chat._AGENT_SEM.release()

    asyncio.run(scenario())
