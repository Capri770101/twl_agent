"""engine/budget.py —— LLM token 成本预算（P6.2，Redis 计数器，best-effort）。

设计要点：
- 日级预算：全局（global）+ 每用户（user）两个维度；``0`` 表示不限制。
- 依赖 Redis（复用 settings.redis_url）；Redis 不可用或未配置时**静默放行**（不强制），
  保证故障不影响主链路，仅失去预算约束。
- 计为 best-effort：调用前做「已用量 ≥ 预算」拦截；调用后按真实 usage 累加。
- 同步 Redis 客户端（llm.py 本身是同步调用，且多在 asyncio.to_thread 中执行，不阻塞事件循环）。

对外函数：
- ``check(user_id) -> (allowed: bool, reason: str | None)``
- ``record(user_id, prompt_tokens, completion_tokens)``
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from backend.config import settings

logger = logging.getLogger('budget')
_make_client: Callable[[], object | None] = None

def _default_client() -> object | None:
    if not settings.redis_url:
        return None
    try:
        import redis
        return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=settings.redis_socket_timeout, decode_responses=True)
    except Exception:
        logger.warning('[budget] Redis 不可用，预算约束降级为不强制')
        return None

def _client() -> object | None:
    if _make_client is not None:
        return _make_client()
    return _default_client()

def _day_key(suffix: str) -> str:
    return f'llm:budget:{suffix}:{datetime.date.today().isoformat()}'

def _ttl_seconds() -> int:
    """距当天结束的剩余秒数（用于 key 过期）。"""
    tomorrow = datetime.datetime.combine(datetime.date.today() + datetime.timedelta(days=1), datetime.time.min)
    delta = tomorrow - datetime.datetime.now()
    return max(1, int(delta.total_seconds()))

def check(user_id: str | None) -> tuple[bool, str | None]:
    """返回 (是否允许, 超限原因)。未启用预算或无 Redis 时一律放行。"""
    if not settings.llm_cost_enabled:
        return (True, None)
    client = _client()
    if client is None:
        return (True, None)
    try:
        if settings.llm_global_daily_token_budget:
            used = int(client.get(_day_key('global')) or 0)
            if used >= settings.llm_global_daily_token_budget:
                return (False, 'global')
        if user_id and settings.llm_user_daily_token_budget:
            used = int(client.get(_day_key(f'user:{user_id}')) or 0)
            if used >= settings.llm_user_daily_token_budget:
                return (False, 'user')
    except Exception:
        logger.warning('[budget] 读取用量失败，降级放行')
        return (True, None)
    return (True, None)

def record(user_id: str | None, prompt_tokens: int, completion_tokens: int) -> None:
    """调用成功后累加 token 用量（日级过期）。"""
    if not settings.llm_cost_enabled:
        return
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    if total <= 0:
        return
    client = _client()
    if client is None:
        return
    ttl = _ttl_seconds()
    try:
        if settings.llm_global_daily_token_budget:
            key = _day_key('global')
            client.incrby(key, total)
            client.expire(key, ttl)
        if user_id and settings.llm_user_daily_token_budget:
            key = _day_key(f'user:{user_id}')
            client.incrby(key, total)
            client.expire(key, ttl)
    except Exception:
        logger.warning('[budget] 记录用量失败（忽略）')
