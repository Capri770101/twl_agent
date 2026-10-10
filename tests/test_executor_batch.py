"""The executor schedules capabilities without knowing application business rules."""
import asyncio
import json

from agent.orchestration.executor import execute_batch


def test_batch_preserves_index_order_and_defers_dependent_tools():
    entered = set()
    both = asyncio.Event()
    order = []

    async def execute(name, args, context):
        assert context == {'owner': 'u'}
        if name in {'slow', 'fast'}:
            entered.add(name)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            if name == 'slow':
                await asyncio.sleep(.02)
        else:
            assert entered == {'slow', 'fast'}
        order.append(name)
        return name, 'ok'

    calls = [{'name': name, 'arguments': {'x': index}} for index, name in
             enumerate(['slow', 'respond', 'deferred', 'fast'])]
    results, response = asyncio.run(execute_batch(
        calls, {'owner': 'u'}, execute=execute, terminal_tools=frozenset({'respond'}),
        deferred_tools=frozenset({'deferred'}), on_error=lambda name, exc: 'error'))
    assert response == {'x': 1}
    assert [results[i] for i in sorted(results)] == [
        ('slow', 'ok'), (json.dumps(response, ensure_ascii=False), 'terminal'),
        ('deferred', 'ok'), ('fast', 'ok')]
    assert order[-1] == 'deferred'
    assert 'respond' not in order


def test_concurrent_failure_does_not_cancel_other_tool():
    async def execute(name, args, context):
        if name == 'bad':
            raise RuntimeError('private secret')
        return 'good', 'ok'

    async def scenario():
        return await execute_batch(
            [{'name': 'bad', 'arguments': {}}, {'name': 'good', 'arguments': {}}], {},
            execute=execute, terminal_tools=frozenset(), deferred_tools=frozenset(),
            on_error=lambda name, exc: 'fixed error code')
    results, response = asyncio.run(scenario())
    assert response is None
    assert results == {0: ('fixed error code', 'error'), 1: ('good', 'ok')}
