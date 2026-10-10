"""Observable contracts of the callback-driven ReAct loop."""
import asyncio
from types import SimpleNamespace

import pytest

from agent.orchestration.executor import execute_react_loop


def run_loop(messages, *, get_message, parse_calls=lambda msg: msg.tool_calls or [], execute=None,
             guard=lambda tool_log, reply, zero: None, on_event=None,
             max_iterations=3, deadline=100, clock=lambda: 0):
    async def default_execute(name, args, context):
        return name, 'ok'
    return asyncio.run(execute_react_loop(
        messages, user_message='hello', max_iterations=max_iterations,
        deadline=deadline, min_iteration_budget=8, stage_label='test',
        get_message=get_message, parse_calls=parse_calls,
        execute=execute or default_execute, tool_context={'owner': 'u'},
        terminal_tools=frozenset({'respond'}), deferred_tools=frozenset({'deferred'}),
        on_error=lambda name, exc: 'fixed_error',
        make_record=lambda call, result, status: SimpleNamespace(
            name=call['name'], status=status, result=result),
        guard=guard, on_event=on_event, clock=clock))


def test_deadline_and_model_exception_never_expose_internal_error():
    def forbidden_message(messages, remaining):
        raise AssertionError('deadline should prevent model call')
    outcome = run_loop([], get_message=forbidden_message, deadline=7)
    assert '初步建议' in outcome.reply
    assert outcome.new_messages == [{'role': 'user', 'content': 'hello'}]

    def failing_message(messages, remaining):
        raise RuntimeError('private endpoint and key')
    outcome = run_loop([], get_message=failing_message)
    assert outcome.reply == '抱歉，我这边服务暂时开小差了，请稍后再试一次～'
    assert 'private' not in outcome.reply


def test_cancelled_tool_batch_propagates_and_leaves_no_result():
    started = asyncio.Event()
    async def blocked_tool(name, args, context):
        started.set()
        await asyncio.Future()
    calls = [{'id': '1', 'name': 'blocked', 'arguments': {}}]
    async def scenario():
        messages = []
        task = asyncio.create_task(execute_react_loop(
            messages, user_message='hello', max_iterations=1, deadline=100,
            min_iteration_budget=8, stage_label='test',
            get_message=lambda transcript, remaining: SimpleNamespace(content='', tool_calls=calls),
            parse_calls=lambda msg: msg.tool_calls, execute=blocked_tool,
            tool_context={'owner': 'u'}, terminal_tools=frozenset(), deferred_tools=frozenset(),
            on_error=lambda name, exc: 'error',
            make_record=lambda call, result, status: (call, result, status),
            guard=lambda log, reply, zero: None, clock=lambda: 0))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
def test_tool_transcript_preserves_original_indices_and_terminal_is_private():
    calls = [{'id': str(i), 'name': name, 'arguments': {'reply': 'done'} if name == 'respond' else {}}
             for i, name in enumerate(('slow', 'respond', 'deferred', 'fast'))]
    replies = [SimpleNamespace(content='', tool_calls=calls)]
    async def execute(name, args, context):
        if name == 'slow':
            await asyncio.sleep(.01)
        return name, 'ok'
    events = []
    transcript = [{'role': 'system', 'content': 'context'}]
    outcome = run_loop(transcript, get_message=lambda messages, remaining: replies.pop(0),
                       execute=execute, on_event=events.append)
    assert outcome.response == {'reply': 'done'}
    assert [msg['tool_call_id'] for msg in outcome.new_messages if msg['role'] == 'tool'] == ['0', '1', '2', '3']
    assert [record.name for record in outcome.tool_log] == ['slow', 'deferred', 'fast']
    assert [event['name'] for event in events] == ['slow', 'deferred', 'fast']


def test_terminal_guard_retry_is_transient_and_zero_tool_guard_also_runs():
    replies = [SimpleNamespace(content='', tool_calls=[{'id': '1', 'name': 'respond',
                                                          'arguments': {'reply': 'unverified'}}]),
                SimpleNamespace(content='still unverified', tool_calls=[]),
                SimpleNamespace(content='verified', tool_calls=[])]
    guards = []
    def guard(log, reply, zero):
        guards.append((reply, zero))
        return ('evidence', 'check the tool first') if len(guards) < 3 else None
    transcript = [{'role': 'system', 'content': 'context'}]
    outcome = run_loop(transcript, get_message=lambda messages, remaining: replies.pop(0), guard=guard)
    assert outcome.response is None
    assert outcome.reply == 'verified'
    assert guards == [('unverified', False), ('still unverified', True), ('verified', True)]
    assert [item['content'] for item in transcript if item['role'] == 'user'] == [
        'check the tool first', 'check the tool first']
    assert all(item['role'] != 'assistant' or item['content'] != 'still unverified'
               for item in outcome.new_messages)
