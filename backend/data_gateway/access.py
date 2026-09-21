"""请求级数据授权。上下文只由服务端建立，不接受模型参数覆盖。"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import os
import re

from backend.config import settings

_SOURCES: ContextVar[frozenset[str] | None] = ContextVar('platform_sources', default=None)
_SOURCE_ID = re.compile(r'^[a-zA-Z0-9_]+$')


def configured_sources() -> list[str]:
    sources = set()
    for key, value in os.environ.items():
        for prefix in ('PLATFORM_DB_', 'PLATFORM_API_'):
            if key.startswith(prefix) and key.endswith('_URL') and value.strip():
                source = key[len(prefix):-4].lower()
                if _SOURCE_ID.fullmatch(source):
                    sources.add(source)
    return sorted(sources)


def sources_for_platform(platform_id: str | None) -> frozenset[str]:
    """映射缺失/错误即拒绝；单源兼容需要显式开启。"""
    configured = frozenset(configured_sources())
    raw = settings.PLATFORM_SOURCE_ACCESS.strip()
    if not raw:
        if settings.PLATFORM_SINGLE_SOURCE_COMPAT and len(configured) == 1:
            return configured
        return frozenset()
    try:
        mapping = json.loads(raw)
        if not isinstance(mapping, dict):
            return frozenset()
        for key, values in mapping.items():
            if not isinstance(key, str) or not isinstance(values, list):
                return frozenset()
            if any(not isinstance(v, str) or not _SOURCE_ID.fullmatch(v) for v in values):
                return frozenset()
        return frozenset(s.lower() for s in mapping.get(platform_id, [])) & configured
    except (ValueError, TypeError):
        return frozenset()


@contextmanager
def platform_scope(platform_id: str | None):
    token = _SOURCES.set(sources_for_platform(platform_id))
    try:
        yield
    finally:
        _SOURCES.reset(token)


def scoped_agent_run(func):
    """在线程内建立上下文；to_thread 会将其传给同步工具。"""
    @wraps(func)
    async def wrapped(*args, platform_id=None, **kwargs):
        with platform_scope(platform_id):
            return await func(*args, **kwargs)
    return wrapped


def visible_sources() -> list[str]:
    scope = _SOURCES.get()
    return configured_sources() if scope is None else sorted(scope)


def cache_scope() -> tuple[str, ...] | None:
    scope = _SOURCES.get()
    return None if scope is None else tuple(sorted(scope))


def require_source(source_id: str) -> None:
    from backend.execution import checkpoint
    checkpoint()
    scope = _SOURCES.get()
    if scope is not None and source_id.lower() not in scope:
        raise PermissionError('当前平台无权访问该数据源')


def require_public_entity(source_id: str, entity: str) -> None:
    require_source(source_id)
    if entity not in ('plan', 'shop'):
        raise PermissionError('当前未开放订单和用户等私有实体查询')
