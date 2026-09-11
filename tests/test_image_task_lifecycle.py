"""生图任务生命周期的回归测试（J-1）。

背景
----
`create_image_task` 曾经使用 `asyncio.create_task` 调度生图协程，而该函数经 ReAct
工具链调用时跑在 `arun()` 内 `asyncio.run(...)` 的**临时事件循环**上：`run()` 一返回，
循环即被销毁并取消挂起任务；而 `CancelledError` 继承 `BaseException`，原来的
`except Exception` 抓不到它 —— 任务永远停在 `processing`（mock 分支是同步实现，
所以本地联调看不出来，只有真实生图才暴露）。

修复方案：把生图任务提交到 `_IMAGE_EXECUTOR`（独立线程 + 自带事件循环）。
本测试锁住这一语义，防止有人改回 `create_task`。
"""
from __future__ import annotations

import asyncio
import time

from backend.storage import tasks as task_store


def test_image_task_survives_temp_loop(monkeypatch) -> None:
    """在临时循环里创建任务，循环销毁后任务仍应在独立线程中跑完。"""
    done: list[str] = []

    async def fake_generate(task_id: str, prompt: str) -> None:
        # 含真实 await 点，模拟生图的 IO 等待
        await asyncio.sleep(0.3)
        done.append(task_id)

    monkeypatch.setattr(task_store, '_generate_image_async', fake_generate)
    monkeypatch.setattr(task_store, '_save_task', lambda *a, **k: None)
    monkeypatch.setattr(task_store, '_update_task', lambda *a, **k: None)

    async def submit_in_temp_loop() -> None:
        task_store._IMAGE_EXECUTOR.submit(task_store._run_image_task, 'T_TEST', 'rose')

    asyncio.run(submit_in_temp_loop())      # 复现：临时循环创建后立即销毁
    time.sleep(0.8)                          # 等后台线程跑完
    assert 'T_TEST' in done, '生图任务被临时循环销毁了（J-1 回归）'


def test_create_image_task_no_longer_uses_create_task() -> None:
    """静态断言：源码中不应再出现 asyncio.create_task 调度生图。"""
    import inspect

    source = inspect.getsource(task_store.create_image_task)
    assert 'asyncio.create_task' not in source
    assert '_IMAGE_EXECUTOR.submit' in source


def test_run_image_task_catches_cancelled_error() -> None:
    """兜底：CancelledError 必须被显式处理，否则任务会静默卡住。"""
    import inspect

    source = inspect.getsource(task_store._run_image_task)
    assert 'CancelledError' in source
