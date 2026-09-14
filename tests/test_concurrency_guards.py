"""P2 并发护栏测试：生图槽位有界 + 智能体并发槽位快速失败（离线，不连 DB）。"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings
from backend.routers import chat
from backend.storage import tasks


def test_config_defaults_present():
    assert settings.AGENT_MAX_CONCURRENCY >= 1
    assert settings.IMAGE_TASK_MAX_WORKERS >= 1
    assert settings.IMAGE_TASK_QUEUE_MAX >= 1


def test_image_executor_bounded():
    # 线程池 worker 数应由配置决定，而不是写死
    assert tasks._IMAGE_EXECUTOR._max_workers == max(1, settings.IMAGE_TASK_MAX_WORKERS)


def test_image_slot_reserve_then_fail_fast(monkeypatch):
    monkeypatch.setattr(tasks, "_IMAGE_SLOTS", threading.BoundedSemaphore(1))
    assert tasks._try_reserve_image_slot() is True
    # 满 → 非阻塞拿不到，快速失败（不排队）
    assert tasks._try_reserve_image_slot() is False
    tasks._release_image_slot()
    assert tasks._try_reserve_image_slot() is True


def test_image_slot_double_release_safe(monkeypatch):
    monkeypatch.setattr(tasks, "_IMAGE_SLOTS", threading.BoundedSemaphore(1))
    tasks._release_image_slot()  # 空信号量上 release 不该抛


def test_agent_slot_fails_fast_when_busy(monkeypatch):
    monkeypatch.setattr(chat, "_AGENT_SEM", asyncio.Semaphore(1))

    async def scenario():
        assert await chat._acquire_agent_slot() is True
        # 已占满 → 短超时后返回 False（快速失败），而不是无限干等
        assert await chat._acquire_agent_slot() is False
        chat._AGENT_SEM.release()
        assert await chat._acquire_agent_slot() is True

    asyncio.run(scenario())
