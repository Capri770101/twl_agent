"""engine/circuit_breaker.py —— 通用熔断器（进程内，线程安全）。

状态机：
- CLOSED：正常放行；记录连续失败数。
- OPEN：失败数达阈值后进入，一段时间（open_seconds）内直接拒绝（快速失败）。
- HALF_OPEN：open 超时后放行一次探测；成功则回 CLOSED，失败则回 OPEN。

设计要点：
- 纯内存实现，零外部依赖，单进程即可用；多 worker 场景建议后续接 Redis 共享状态。
- 通过注入 ``clock`` 便于测试（无需真实等待）。
"""

from __future__ import annotations

import threading
import time

OPEN = "open"
HALF_OPEN = "half_open"
CLOSED = "closed"


class CircuitBreaker:
    """基于连续失败阈值的熔断器。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        clock: callable = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.open_seconds = float(open_seconds)
        self._clock = clock
        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """当前是否允许放行一次调用。"""
        with self._lock:
            if self._state == OPEN:
                if self._clock() - self._opened_at >= self.open_seconds:
                    self._state = HALF_OPEN
                    return True
                return False
            return True

    def on_success(self) -> None:
        """调用成功：重置失败计数并回到 CLOSED。"""
        with self._lock:
            self._failures = 0
            self._state = CLOSED

    def on_failure(self) -> None:
        """调用失败：累计失败；达阈值则进入 OPEN。"""
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = OPEN
                self._opened_at = self._clock()
