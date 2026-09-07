"""调用监控埋点模块（observability）。

设计原则
--------
- **best-effort**：所有写入都被 try/except 包住，任何异常只记 warning，
  绝不影响 /chat 主链路（监控挂了也不能让业务挂）。
- **零侵入**：数据在 chat 层聚合后写入，不依赖 agent 线程池内部的 contextvar
  （run() 在 executor 线程跑，ContextVar 跨线程不传递，故 record_call_end /
  record_tool_call 支持显式传入 cid 兜底）。
- **预留钩子**：llm.py 里已存在 `observability.record_llm(...)` 调用，本模块
  实现为无副作用占位，保留未来按需扩展 token 级统计的空间。

数据落 `call_logs` / `tool_call_logs`（见 backend/storage/db.py 的 _SCHEMA）。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from backend.storage.db import transaction

logger = logging.getLogger('observability')

# 当前请求的 call_log id；在异步请求上下文（chat.py）中设置/读取。
_call_id: ContextVar[int | None] = ContextVar('call_id', default=None)


def set_call_context(cid: int | None) -> None:
    """手动设置当前上下文的 call_log id（SSE 流场景用，绕过 contextvar 跨协程问题）。"""
    _call_id.set(cid)


def get_call_context() -> int | None:
    return _call_id.get()


def record_call_start(
    user_id: str,
    platform_id: str | None,
    session_id: str | None,
    model: str,
) -> int | None:
    """在一次对话入口调用，写入 call_logs 一行（status='pending'），返回 id。

    返回 None 表示写入失败（不影响主流程）。
    """
    try:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO call_logs (user_id, platform_id, session_id, model, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', NOW()) RETURNING id",
                (user_id, platform_id, session_id, model),
            )
            row = cur.fetchone()
            cid = int(row['id']) if row else None
        set_call_context(cid)
        return cid
    except Exception as exc:  # noqa: BLE001 — 监控失败绝不影响业务
        logger.warning('[observability] record_call_start 失败: %s', exc)
        return None


def record_call_end(
    status: str,
    latency_ms: int,
    error: str | None = None,
    tool_calls: int = 0,
    cid: int | None = None,
) -> None:
    """在一次对话出口（含异常分支）调用，更新 call_logs 的状态/耗时/错误/工具数。"""
    cid = cid if cid is not None else get_call_context()
    if cid is None:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "UPDATE call_logs SET status=?, latency_ms=?, error=?, tool_calls=? WHERE id=?",
                (status, int(latency_ms), error, int(tool_calls), cid),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[observability] record_call_end 失败 cid=%s: %s', cid, exc)
    finally:
        # 仅在用的是上下文变量时清理，避免误清显式传入的调用方状态
        if cid is None or cid == get_call_context():
            set_call_context(None)


def record_tool_call(
    tool_name: str,
    status: str,
    latency_ms: int = 0,
    error: str | None = None,
    cid: int | None = None,
) -> None:
    """记录单次工具调用（工具分布统计用）。必须在对应 call 的 context 内调用。"""
    cid = cid if cid is not None else get_call_context()
    if cid is None:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO tool_call_logs (call_log_id, tool_name, status, latency_ms, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, NOW())",
                (cid, tool_name, status, int(latency_ms), error),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning('[observability] record_tool_call 失败 cid=%s tool=%s: %s', cid, tool_name, exc)


def record_llm(prompt_tokens: int = 0, completion_tokens: int = 0, error: bool = False) -> None:
    """预留钩子（llm.py 每轮调用）。

    当前 token 级统计未在指标面板需求内，且跨线程无法安全关联到 call_log，
    故保持无副作用。如需启用，可在此按显式 cid 累加 token。
    """
    return
