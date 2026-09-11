"""进程内限流器的行为测试。

为什么重要
----------
J-3：项目此前**没有任何限流**，对按 token 计费的服务是直接的资金风险。
本文件锁住限流器的核心语义（窗口、隔离、过期、内存有界）。
"""
from __future__ import annotations

import time

from backend.rate_limit import FixedWindowLimiter


def test_allows_up_to_limit_then_blocks() -> None:
    limiter = FixedWindowLimiter(limit=3, window_seconds=60)
    assert [limiter.allow('u')[0] for _ in range(5)] == [True, True, True, False, False]


def test_retry_after_positive_when_blocked() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)
    limiter.allow('u')
    allowed, retry_after = limiter.allow('u')
    assert allowed is False
    assert retry_after > 0


def test_first_call_not_blocked_returns_zero_retry() -> None:
    limiter = FixedWindowLimiter(limit=5, window_seconds=60)
    allowed, retry_after = limiter.allow('u')
    assert allowed is True
    assert retry_after == 0


def test_keys_are_isolated() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=60)
    assert limiter.allow('a')[0] is True
    assert limiter.allow('b')[0] is True


def test_window_expiry_resets_counter() -> None:
    limiter = FixedWindowLimiter(limit=1, window_seconds=1.0)
    assert limiter.allow('u')[0] is True
    assert limiter.allow('u')[0] is False
    time.sleep(1.05)
    assert limiter.allow('u')[0] is True


def test_expired_keys_evicted_to_bound_memory() -> None:
    """key 数量超过 max_keys 且已过期时应被清理，避免内存无限增长。"""
    limiter = FixedWindowLimiter(limit=1, window_seconds=1.0, max_keys=1000)
    for i in range(1500):
        limiter.allow(f'k{i}')
    assert len(limiter._counters) > 1000
    time.sleep(1.05)                       # 让所有窗口过期
    limiter.allow('trigger')               # 触发清理
    assert len(limiter._counters) < 1500
