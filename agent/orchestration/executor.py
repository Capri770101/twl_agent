"""Business-agnostic execution of a single model tool-call batch.

The caller owns authorization/context, tool-result evidence, and user-visible output.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger('agent')


@dataclass
class LoopResult:
    response: dict[str, Any] | None
    reply: str
    tool_log: list[Any]
    new_messages: list[dict[str, Any]]


async def execute_react_loop(
    messages: list[dict[str, Any]], *, user_message: str, max_iterations: int,
    deadline: float, min_iteration_budget: float, stage_label: str,
    get_message: Callable[[list[dict[str, Any]], float], Any],
    parse_calls: Callable[[Any], list[dict[str, Any]]],
    execute: Callable[[str, dict[str, Any], Mapping[str, Any]], Awaitable[tuple[str, str]]],
    tool_context: Mapping[str, Any], terminal_tools: frozenset[str],
    deferred_tools: frozenset[str], on_error: Callable[[str, BaseException], str],
    make_record: Callable[[dict[str, Any], str, str], Any],
    guard: Callable[[list[Any], str, bool], tuple[str, str] | None],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> LoopResult:
    """Run only model/tool/guard orchestration, never publish unvalidated text.

    Business policy and hard authorization stay in the injected guard/execute
    callbacks. The caller owns post-processing, persistence and final SSE output.
    """
    tool_log: list[Any] = []
    response: dict[str, Any] | None = None
    reply = ''
    new_messages: list[dict[str, Any]] = [{'role': 'user', 'content': user_message}]
    for turn in range(1, max_iterations + 1):
        remaining = deadline - clock()
        if remaining <= min_iteration_budget:
            logger.warning('[agent] 时间预算耗尽（剩余 %.1fs），第 %d 轮主动收尾', remaining, turn)
            reply = reply or '这个问题有点绕，我先给你一个初步建议；要更细致的可以咱们分步慢慢聊～'
            break
        logger.info('[agent] ReAct 第 %d/%d 轮 阶段=%s', turn, max_iterations, stage_label)
        try:
            msg = get_message(messages, remaining)
        except Exception:
            logger.exception('[agent] LLM 调用失败')
            reply = '抱歉，我这边服务暂时开小差了，请稍后再试一次～'
            break
        calls = parse_calls(msg)
        if calls:
            assistant_message = {'role': 'assistant', 'content': getattr(msg, 'content', '') or '',
                                 'tool_calls': [{'id': tc['id'], 'type': 'function',
                                                 'function': {'name': tc['name'], 'arguments': json.dumps(tc['arguments'], ensure_ascii=False)}}
                                                for tc in calls]}
            messages.append(assistant_message)
            new_messages.append({**assistant_message, 'content': ''})
            results, batch_response = await execute_batch(
                calls, tool_context, execute=execute, terminal_tools=terminal_tools,
                deferred_tools=deferred_tools, on_error=on_error)
            if batch_response is not None:
                response = batch_response
            for index in sorted(results):
                call = calls[index]
                result, status = results[index]
                if status != 'terminal':
                    tool_log.append(make_record(call, result, status))
                    if on_event:
                        on_event({'event': 'tool_call', 'name': call['name'], 'status': status})
                tool_message = {'role': 'tool', 'content': result, 'tool_call_id': call.get('id', '')}
                messages.append(tool_message)
                new_messages.append(tool_message)
            if response is not None:
                hit = guard(tool_log, str((response or {}).get('reply') or reply or ''), False)
                if hit:
                    messages.append({'role': 'user', 'content': hit[1]})
                    response = None
                    continue
                break
            continue
        reply = getattr(msg, 'content', '') or ''
        hit = guard(tool_log, reply, True)
        if hit:
            messages.append({'role': 'assistant', 'content': reply})
            messages.append({'role': 'user', 'content': hit[1]})
            reply = ''
            continue
        messages.append({'role': 'assistant', 'content': reply})
        break
    else:
        if any(tc.status == 'ok' for tc in tool_log):
            reply = reply or '我已经为你整理好相关结果啦，请查看下方卡片～'
        else:
            reply = reply or '抱歉，我思考得太久啦，请简化需求或分步骤再问我～'
    return LoopResult(response, reply, tool_log, new_messages)


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
