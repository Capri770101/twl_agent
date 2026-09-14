"""P0 回归门：流式入口绝不能因内部异常而永久挂起。

背景：`ReActAgent.arun_stream` 的 `_run` 协程若抛异常且**不补 `queue.put(None)`**，
消费端 `await queue.get()` 会永久阻塞 —— SSE 挂死、用户转圈不停。

本测试用 `asyncio.wait_for` 兜底语义：若实现回归（挂起），wait_for 抛 `TimeoutError`
使测试**失败**，而不是把整个测试进程吊死。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.agent import ReActAgent


async def _consume(agent: ReActAgent, user_id: str = "u", message: str = "hi", sid: str = "s") -> list[dict]:
    events: list[dict] = []
    async for evt in agent.arun_stream(user_id, message, sid):
        events.append(evt)
    return events


def test_stream_terminates_when_run_raises(monkeypatch):
    """run() 抛异常 → 流必须给出 error 事件并正常结束（不挂起）。"""
    agent = ReActAgent()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "run", boom)
    events = asyncio.run(asyncio.wait_for(_consume(agent), timeout=5))
    assert events, "流应至少产出一个事件"
    assert any(e.get("event") == "error" for e in events), "异常应转为 error 事件"
    assert events[-1].get("event") == "error"


def test_stream_happy_path_terminates(monkeypatch):
    """正常返回 → 流给出 done 事件并结束。"""
    agent = ReActAgent()

    async def fake_run(*args, **kwargs):
        return SimpleNamespace(session_id="s1")

    monkeypatch.setattr(agent, "run", fake_run)
    events = asyncio.run(asyncio.wait_for(_consume(agent), timeout=5))
    assert any(e.get("event") == "done" for e in events)
    assert events[-1].get("event") == "done"
