"""Business-agnostic execution of a single model tool-call batch.

The caller owns authorization/context, tool-result evidence, and user-visible output.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger('agent')


async def execute_batch(
    calls: Sequence[dict[str, Any]],
    context: Mapping[str, Any],
    *,
    execute: Callable[[str, dict[str, Any], Mapping[str, Any]], Awaitable[tuple[str, str]]],
    terminal_tools: frozenset[str],
    deferred_tools: frozenset[str],
    on_error: Callable[[str, BaseException], str],
) -> tuple[dict[int, tuple[str, str]], dict[str, Any] | None]:
    """Execute regular calls concurrently, then deferred calls; return indexed results.

    Terminal calls are data, not executable tools. Keeping indices allows the caller
    to append results to the model transcript in the original call order. Runtime
    authorization must stay inside ``execute``; a configured route is not permission.
    """
    results: dict[int, tuple[str, str]] = {}
    regular: list[int] = []
    deferred: list[int] = []
    response: dict[str, Any] | None = None
    for index, call in enumerate(calls):
        name = call.get('name') or ''
        if name in terminal_tools:
            response = call['arguments']
            results[index] = (json.dumps(response, ensure_ascii=False), 'terminal')
        else:
            (deferred if name in deferred_tools else regular).append(index)

    if len(regular) > 1:
        gathered = await asyncio.gather(
            *(execute(calls[index]['name'], calls[index]['arguments'], context)
              for index in regular),
            return_exceptions=True,
        )
        for index, item in zip(regular, gathered):
            if isinstance(item, BaseException):
                logger.exception('[agent] 并发工具执行异常: %s', calls[index]['name'], exc_info=item)
                results[index] = (on_error(calls[index]['name'], item), 'error')
            else:
                results[index] = item
        logger.info('[agent] 同轮并发执行 %d 个工具: %s',
                    len(regular), [calls[index]['name'] for index in regular])
    elif regular:
        index = regular[0]
        try:
            results[index] = await execute(calls[index]['name'], calls[index]['arguments'], context)
        except Exception as exc:
            logger.exception('[agent] 单工具执行异常: %s', calls[index]['name'])
            results[index] = (on_error(calls[index]['name'], exc), 'error')

    for index in deferred:
        try:
            results[index] = await execute(calls[index]['name'], calls[index]['arguments'], context)
        except Exception as exc:
            logger.exception('[agent] 延迟工具执行异常: %s', calls[index]['name'])
            results[index] = (on_error(calls[index]['name'], exc), 'error')
    return results, response
