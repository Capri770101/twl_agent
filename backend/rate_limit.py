"""进程内请求限流（固定窗口计数）。

设计取舍
--------
- **为什么用内存实现**：当前为单实例部署，内存实现零依赖、无网络往返，避免仅为一个
  限流再引入 Redis（也为没有 Redis 的部署留出可用的默认护栏）。内存计数随进程重启清零，
  对「防刷/防跑量」这一目的完全够用。
- **局限**：多实例横向扩容时各实例独立计数，实际总配额 ≈ 实例数 × 单实例配额，
  并非全局精确。若后续需要全局精确限流，把 :class:`FixedWindowLimiter` 换成 Redis
  后端即可，对外函数 ``check_rate_limit(key)`` 契约不变。
- **维度**：由调用方决定 key（如 ``chat:<user_id>``），本模块只管计数与判定。

与 token 预算护栏（agent/engine/budget.py）的分工：本模块按**请求次数**限流，
budget.py 按**token 用量**做日级预算；两者互补，共同构成成本护栏。
"""
from __future__ import annotations

import logging
import threading
import time

from backend.config import settings

logger = logging.getLogger('rate_limit')


class FixedWindowLimiter:
    """固定窗口计数器限流器（线程安全）。

    以 ``window_seconds`` 为一个窗口，窗口内计数超过 ``limit`` 即拒绝；
    窗口到期后计数归零重新开始。相比滑动窗口实现更简单、无每请求的时间戳列表开销，
    对「防刷」场景的边界抖动不敏感。
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 20000) -> None:
        """初始化限流器。

        Args:
            limit: 单窗口允许的最大请求数。
            window_seconds: 窗口长度（秒），默认 60。
            max_keys: 计数表条目上限，超过时触发过期清理，防止内存无限增长。
        """
        self._limit = max(1, int(limit))
        self._window = max(1.0, float(window_seconds))
        self._max_keys = max(1000, int(max_keys))
        # key -> (window_start_monotonic, count)
        self._counters: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """登记一次访问并判定是否放行。

        Args:
            key: 限流维度标识（如 ``chat:user_123``）。

        Returns:
            ``(是否允许, retry_after_seconds)``：允许时第二项为 0；
            拒绝时为建议的等待秒数（用于 HTTP ``Retry-After``）。
        """
        now = time.monotonic()
        with self._lock:
            start, count = self._counters.get(key, (now, 0))
            if now - start >= self._window:
                # 窗口已过期，重新开窗
                start, count = now, 0
            count += 1
            self._counters[key] = (start, count)
            if len(self._counters) > self._max_keys:
                self._evict_expired(now)
            if count > self._limit:
                retry_after = max(1, int(self._window - (now - start)) + 1)
                return False, retry_after
            return True, 0

    def _evict_expired(self, now: float) -> None:
        """清理已过期窗口的计数条目（调用方需已持锁）。"""
        expired = [k for k, (start, _) in self._counters.items() if now - start >= self._window]
        for k in expired:
            self._counters.pop(k, None)
        logger.debug('[rate_limit] 清理过期计数 key=%d 剩余=%d', len(expired), len(self._counters))


_limiter: FixedWindowLimiter | None = None
_init_lock = threading.Lock()


def _get_limiter() -> FixedWindowLimiter:
    """惰性构造全局限流器（首次调用时按当前配置初始化）。"""
    global _limiter
    if _limiter is None:
        with _init_lock:
            if _limiter is None:
                _limiter = FixedWindowLimiter(settings.RATE_LIMIT_PER_MINUTE, window_seconds=60.0)
                logger.info('[rate_limit] 已启用：%d 次/分钟', settings.RATE_LIMIT_PER_MINUTE)
    return _limiter


def check_rate_limit(key: str) -> tuple[bool, int]:
    """对指定维度做一次限流检查。

    Args:
        key: 限流维度标识。

    Returns:
        ``(是否允许, retry_after_seconds)``；未启用限流时一律放行 ``(True, 0)``。
    """
    if not settings.RATE_LIMIT_ENABLED:
        return True, 0
    return _get_limiter().allow(key)
