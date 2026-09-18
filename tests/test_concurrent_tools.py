"""同轮工具并发执行 + 同步工具不阻塞事件循环（2026-09-18，A-4）。

为什么需要：
  · 旧实现逐个 `await execute_tool(...)`，模型一次发多个调用时用户要为每一次
    网络往返分别等待；并发后总耗时 ≈ 最慢的那个。
  · 同步工具（如 platform_db_query_entity 走同步 httpx.get）**直接调用会阻塞整个
    事件循环** —— 一个用户执行工具期间，其它并发请求全部排队。必须丢线程池。

本测试锁定这两条，防止回退。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import toolkit


def _register_tmp(name: str, func):
    """注册一个临时工具（测试后清理）。"""
    toolkit.register_tool(name=name, description='test-only', parameters={'type': 'object', 'properties': {}})(func)
    return name


def _drop(name: str) -> None:
    toolkit.TOOL_REGISTRY.pop(name, None)


# ── A. 同步工具在线程池执行 ──

def test_sync_tool_runs_off_event_loop():
    """同步工具必须在线程池执行 —— 否则会阻塞事件循环。"""
    main_thread = threading.get_ident()
    seen: dict = {}

    def _ping():
        seen['thread'] = threading.get_ident()
        return 'pong'

    name = _register_tmp('__t_ping', _ping)
    try:
        out, status = asyncio.run(toolkit.execute_tool(name, {}))
        assert status == 'ok'
        assert out == 'pong'
        assert seen['thread'] != main_thread, '同步工具仍在事件循环线程里执行（会阻塞其它请求）'
    finally:
        _drop(name)


def test_sync_tool_result_semantics_unchanged():
    """丢线程池后返回值的封装语义不变（dict → JSON 字符串，error 判定照旧）。"""
    def _ok():
        return {'ok': True, 'data': [1, 2]}

    def _bad():
        return {'ok': False, 'error': 'nope'}

    n1 = _register_tmp('__t_ok', _ok)
    n2 = _register_tmp('__t_bad', _bad)
    try:
        out, status = asyncio.run(toolkit.execute_tool(n1, {}))
        assert status == 'ok' and '"data": [1, 2]' in out

        out, status = asyncio.run(toolkit.execute_tool(n2, {}))
        assert status == 'error', 'ok=false 必须判为 error（与线程池无关，防回归）'
    finally:
        _drop(n1)
        _drop(n2)


def test_sync_tool_exception_still_caught():
    """线程池里的异常仍被 execute_tool 兜住，不冒泡到调用方。"""
    def _boom():
        raise ValueError('boom')

    name = _register_tmp('__t_boom', _boom)
    try:
        out, status = asyncio.run(toolkit.execute_tool(name, {}))
        assert status == 'error'
        assert 'boom' in out
    finally:
        _drop(name)


# ── B. 并发确实重叠 ──

def test_concurrent_sync_tools_overlap():
    """三个 0.25s 的同步工具并发执行 → 总耗时 ≈ 0.25s（串行会是 0.75s）。"""
    def _sleep():
        time.sleep(0.25)
        return 'ok'

    name = _register_tmp('__t_sleep', _sleep)

    async def _main() -> float:
        t0 = time.time()
        await asyncio.gather(*(toolkit.execute_tool(name, {}) for _ in range(3)))
        return time.time() - t0

    try:
        cost = asyncio.run(_main())
        assert cost < 0.55, f'同步工具未并发（实测 {cost:.2f}s，串行约 0.75s）'
    finally:
        _drop(name)


def test_concurrent_mixed_sync_async():
    """同步 + 异步工具混在一批里也能并发，且各自结果正确。"""
    def _sync():
        time.sleep(0.25)
        return 'sync-done'

    async def _async_():
        await asyncio.sleep(0.25)
        return 'async-done'

    n1 = _register_tmp('__t_sync', _sync)
    toolkit.register_tool(name='__t_async', description='test-only',
                          parameters={'type': 'object', 'properties': {}})(_async_)
    try:
        async def _main():
            t0 = time.time()
            out = await asyncio.gather(toolkit.execute_tool(n1, {}), toolkit.execute_tool('__t_async', {}))
            return time.time() - t0, out

        cost, out = asyncio.run(_main())
        assert cost < 0.55, f'同步 + 异步混合未并发（实测 {cost:.2f}s）'
        assert out[0][0] == 'sync-done' and out[1][0] == 'async-done'
    finally:
        _drop(n1)
        _drop('__t_async')


# ── C. 主循环分批策略（源码结构守卫）──

def test_deferred_tools_contains_effect_image():
    """生图读同轮 DIY 写入的会话状态，必须排在并发批次之后串行执行。"""
    from agent.agent import _DEFERRED_TOOLS

    assert 'generate_effect_image' in _DEFERRED_TOOLS


def test_run_backfills_in_original_order():
    """run() 必须按原始索引回填结果（sorted(results)），否则 tool 消息顺序错位。"""
    import inspect

    from agent.agent import ReActAgent

    src = inspect.getsource(ReActAgent.run)
    assert 'for i in sorted(results)' in src, \
        '工具结果回填顺序被改动 —— 必须按模型给出的原始顺序，否则 tool 消息与 tool_calls 错位'
    assert 'asyncio.gather' in src, '并发执行逻辑缺失'
    assert 'return_exceptions=True' in src, '单个工具异常会拖垮整批并发结果'


def test_terminal_tools_not_executed_concurrently():
    """终结工具只取参数、不进并发批次（它的参数就是本轮结论）。"""
    import inspect

    from agent.agent import ReActAgent

    src = inspect.getsource(ReActAgent.run)
    assert "'terminal'" in src, '终结工具的特殊处理被移除'
