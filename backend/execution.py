"""跨线程共享的请求期限与协作取消信号。"""
import asyncio
import contextvars
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Execution:
    user_id: str
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)


current: contextvars.ContextVar[Execution | None] = contextvars.ContextVar('execution', default=None)


def checkpoint() -> None:
    state = current.get()
    if state and (state.cancelled.is_set() or time.monotonic() >= state.deadline):
        raise TimeoutError('request execution expired or cancelled')


def remaining(default: float) -> float:
    checkpoint()
    state = current.get()
    return min(default, state.deadline - time.monotonic()) if state else default


def user_id(fallback=None):
    state = current.get()
    return fallback or (state.user_id if state else None)


async def run_worker(executor, factory, uid: str, timeout: float):
    state = Execution(uid, time.monotonic() + timeout)
    context = contextvars.copy_context()

    def work():
        token = current.set(state)
        try:
            checkpoint()
            return asyncio.run(factory())
        finally:
            current.reset(token)

    future = asyncio.get_running_loop().run_in_executor(executor, context.run, work)
    try:
        return await future
    finally:
        state.cancelled.set()
