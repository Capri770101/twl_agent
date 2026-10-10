"""Replay deterministic model/tool traces against the orchestration boundary."""
import asyncio
from types import SimpleNamespace

import pytest

from agent.orchestration.executor import (
    LoopFinishReason, execute_batch_outcomes, execute_react_loop,
)


def replay(turns, *, guard=lambda log, reply, zero: None, max_iterations=3,
           deadline=100, clock=lambda: 0):
    remaining_turns = list(turns)
    observed = []
    messages = [{'role': 'system', 'content': 'safe context'}]

    def model(transcript, remaining):
        observed.append(('model', tuple(m['role'] for m in transcript)))
        return remaining_turns.pop(0)

    async def tool(name, args, context):
        observed.append(('tool', name, context['owner']))
        return name, 'ok'

    result = asyncio.run(execute_react_loop(
        messages, user_message='question', max_iterations=max_iterations,
        deadline=deadline, min_iteration_budget=8, stage_label='test',
        get_message=model, parse_calls=lambda msg: msg.tool_calls or [],
        execute=tool, tool_context={'owner': 'u'},
        terminal_tools=frozenset({'respond'}), deferred_tools=frozenset(),
        on_error=lambda name, exc: 'safe error',
        make_record=lambda call, value, status: SimpleNamespace(name=call['name'], status=status),
        guard=guard, clock=clock,
    ))
    return result, messages, observed


def text(value):
    return SimpleNamespace(content=value, tool_calls=[])


def terminal(value):
    return SimpleNamespace(content='', tool_calls=[
        {'id': 'end', 'name': 'respond', 'arguments': {'reply': value}}])


def tool_turn():
    return SimpleNamespace(content='', tool_calls=[
        {'id': 'lookup', 'name': 'lookup', 'arguments': {}}])


@pytest.mark.parametrize(('turns', 'guard', 'maximum', 'deadline', 'reason', 'count', 'retries'), [
    ([text('hello')], None, 3, 100, LoopFinishReason.TEXT_REPLY, 1, 0),
    ([terminal('done')], None, 3, 100, LoopFinishReason.TERMINAL_TOOL, 1, 0),
    ([text('ignored')], None, 3, 7, LoopFinishReason.DEADLINE, 0, 0),
    ([text('no'), text('yes')], 'reject_once', 3, 100, LoopFinishReason.TEXT_REPLY, 2, 1),
    ([terminal('no'), text('yes')], 'reject_once', 3, 100, LoopFinishReason.TEXT_REPLY, 2, 1),
    ([text('no'), tool_turn()], 'reject_once', 2, 100, LoopFinishReason.MAX_ITERATIONS, 2, 1),
    ([terminal('no')], 'reject_all', 1, 100, LoopFinishReason.GUARD_RETRY_EXHAUSTED, 1, 1),
    ([text('no')], 'reject_all', 1, 100, LoopFinishReason.GUARD_RETRY_EXHAUSTED, 1, 1),
    ([terminal('no')], None, 0, 100, LoopFinishReason.MAX_ITERATIONS, 0, 0),
])
def test_replay_stop_reasons_and_guard_trace(turns, guard, maximum, deadline, reason, count, retries):
    seen = []
    def validator(log, reply, zero):
        seen.append((reply, zero))
        if guard == 'reject_all' or guard == 'reject_once' and len(seen) == 1:
            return 'unsafe', 'retry safely'
        return None
    result, transcript, observed = replay(
        turns, guard=validator, max_iterations=maximum, deadline=deadline)
    assert (result.finish_reason, result.turns, result.guard_retries) == (reason, count, retries)
    assert len([event for event in observed if event[0] == 'model']) == (
        0 if reason == LoopFinishReason.DEADLINE else count)
    assert [msg['content'] for msg in transcript if msg['role'] == 'user'] == ['retry safely'] * retries
    if reason == LoopFinishReason.TERMINAL_TOOL:
        assert result.response == {'reply': 'done'}
    if reason == LoopFinishReason.GUARD_RETRY_EXHAUSTED:
        assert result.response is None


def test_model_error_is_replayable_without_exposing_internal_details():
    def fail_model(transcript, remaining):
        raise RuntimeError('private provider credential')
    result = asyncio.run(execute_react_loop(
        [], user_message='question', max_iterations=2, deadline=100,
        min_iteration_budget=8, stage_label='test', get_message=fail_model,
        parse_calls=lambda msg: msg.tool_calls or [],
        execute=lambda name, args, context: None, tool_context={},
        terminal_tools=frozenset(), deferred_tools=frozenset(),
        on_error=lambda name, exc: 'safe error',
        make_record=lambda call, value, status: None,
        guard=lambda log, reply, zero: None, clock=lambda: 0))
    assert result.finish_reason == LoopFinishReason.MODEL_ERROR
    assert result.turns == 1
    assert 'private' not in result.reply


def test_replay_mixed_tool_batch_has_ordered_typed_outcomes():
    calls = [{'name': name, 'arguments': {'reply': 'ok'} if name == 'respond' else {}}
             for name in ('slow', 'respond', 'deferred', 'fast')]
    order = []
    async def tool(name, args, context):
        if name == 'slow':
            await asyncio.sleep(.01)
        order.append(name)
        return name, 'ok'
    outcomes, response = asyncio.run(execute_batch_outcomes(
        calls, {}, execute=tool, terminal_tools=frozenset({'respond'}),
        deferred_tools=frozenset({'deferred'}), on_error=lambda name, exc: 'error'))
    assert [(outcome.index, outcome.name, outcome.status, outcome.terminal) for outcome in outcomes] == [
        (0, 'slow', 'ok', False), (1, 'respond', 'terminal', True),
        (2, 'deferred', 'ok', False), (3, 'fast', 'ok', False)]
    assert order[-1] == 'deferred'
    assert response == {'reply': 'ok'}


def test_cancelled_child_in_batch_does_not_trigger_deferred_side_effect():
    side_effects = []
    async def tool(name, args, context):
        if name == 'cancel':
            raise asyncio.CancelledError()
        side_effects.append(name)
        return name, 'ok'
    async def scenario():
        await execute_batch_outcomes(
            [{'name': 'cancel', 'arguments': {}}, {'name': 'ordinary', 'arguments': {}},
             {'name': 'deferred', 'arguments': {}}], {}, execute=tool,
            terminal_tools=frozenset(), deferred_tools=frozenset({'deferred'}),
            on_error=lambda name, exc: 'should not suppress cancellation')
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert 'deferred' not in side_effects
