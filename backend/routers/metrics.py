"""调用监控数据接口（/api/metrics），受 DASHBOARD_API_KEY 保护。

鉴权
----
- 所有端点校验 DASHBOARD_API_KEY（settings.DASHBOARD_API_KEY）。
- 支持三种传参：Header `X-Dashboard-Key`、Header `Authorization: Bearer <key>`、
  以及 SSE 的 query `?key=`（EventSource 无法自定义 header）。
- 未配置 DASHBOARD_API_KEY 时一律 503，杜绝面板裸奔。

数据来源
--------
call_logs / tool_call_logs（见 backend/storage/db.py）。所有查询走 db.transaction()，
占位符用 `?`（由连接适配器转 %s，兼容现有仓储写法）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.config import settings
from backend.storage.db import transaction

logger = logging.getLogger('metrics')
router = APIRouter(prefix='/api/metrics', tags=['metrics'])

# date_trunc 允许的第一参数白名单，防止拼接注入
_ALLOWED_BUCKETS = {'minute', 'hour', 'day'}


async def require_dashboard(request: Request) -> None:
    """依赖：校验监控密钥。未配置 → 503；错误 → 401。"""
    if not settings.DASHBOARD_API_KEY:
        raise HTTPException(status_code=503, detail='监控面板未启用：请在 .env 配置 DASHBOARD_API_KEY')
    key = (
        request.headers.get('X-Dashboard-Key')
        or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        or request.query_params.get('key', '')
    )
    if not key or not secrets.compare_digest(key, settings.DASHBOARD_API_KEY):
        raise HTTPException(status_code=401, detail='监控密钥无效')


def _verify(request: Request) -> None:
    # 同步端点内复用（在 async 依赖外也可直接调用）。
    # K-2 修复：与 require_dashboard 保持一致——同时支持 header 与 SSE 的 query `?key=`。
    # 否则 dashboard 用 EventSource（无法自定义 header）连 /api/metrics/stream?key= 时会被判 401。
    if not settings.DASHBOARD_API_KEY:
        raise HTTPException(status_code=503, detail='监控面板未启用：请在 .env 配置 DASHBOARD_API_KEY')
    key = (
        request.headers.get('X-Dashboard-Key')
        or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
        or request.query_params.get('key', '')
    )
    if not key or not secrets.compare_digest(key, settings.DASHBOARD_API_KEY):
        raise HTTPException(status_code=401, detail='监控密钥无效')


@router.get('/summary')
async def summary(request: Request) -> dict[str, Any]:
    """全局 + 近 24h 概览：总量、错误、活跃平台/用户、平均延迟。"""
    _verify(request)
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM call_logs) AS total_calls,
                (SELECT count(*) FROM call_logs WHERE created_at >= NOW() - INTERVAL '24 hours') AS calls_24h,
                (SELECT count(*) FROM call_logs WHERE created_at >= NOW() - INTERVAL '24 hours' AND status='error') AS errors_24h,
                (SELECT count(DISTINCT platform_id) FROM call_logs WHERE created_at >= NOW() - INTERVAL '24 hours' AND platform_id IS NOT NULL) AS active_platforms_24h,
                (SELECT count(DISTINCT user_id) FROM call_logs WHERE created_at >= NOW() - INTERVAL '24 hours') AS active_users_24h,
                (SELECT COALESCE(ROUND(AVG(latency_ms)),0)::bigint FROM call_logs WHERE created_at >= NOW() - INTERVAL '24 hours') AS avg_latency_24h
            """
        ).fetchone()
    return dict(row) if row else {}


@router.get('/calls')
async def calls(
    request: Request,
    hours: int = Query(24, ge=1, le=720),
    bucket: str = Query('hour', pattern='^(minute|hour|day)$'),
) -> list[dict[str, Any]]:
    """调用量时间序列（按小时/天分桶），含每桶错误数。"""
    _verify(request)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT date_trunc(?, created_at) AS bucket,
                   count(*) AS total,
                   count(*) FILTER (WHERE status='error') AS errors
            FROM call_logs
            WHERE created_at >= NOW() - (? || ' hours')::interval
            GROUP BY 1 ORDER BY 1
            """,
            (bucket, hours),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get('/platforms')
async def platforms(request: Request) -> list[dict[str, Any]]:
    """各平台接入与活跃度：累计/近24h调用、独立用户数、最近调用时间、错误数。"""
    _verify(request)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT platform_id,
                   count(*) AS total_calls,
                   count(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS calls_24h,
                   count(DISTINCT user_id) AS distinct_users,
                   max(created_at) AS last_call_at,
                   count(*) FILTER (WHERE status='error') AS errors
            FROM call_logs
            WHERE platform_id IS NOT NULL
            GROUP BY platform_id
            ORDER BY total_calls DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@router.get('/tools')
async def tools(request: Request) -> list[dict[str, Any]]:
    """工具调用分布：各工具累计/成功/失败次数与平均耗时。"""
    _verify(request)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT tool_name,
                   count(*) AS total,
                   count(*) FILTER (WHERE status='ok') AS success,
                   count(*) FILTER (WHERE status='error') AS errors,
                   COALESCE(ROUND(AVG(latency_ms)),0)::bigint AS avg_latency_ms
            FROM tool_call_logs
            GROUP BY tool_name
            ORDER BY total DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@router.get('/speech')
async def speech(request: Request, hours: int = Query(24, ge=1, le=720)) -> dict[str, Any]:
    """语音用量：TTS/ASR 调用数、用量（字符/秒）、缓存命中、错误、平均延迟。

    units 口径：TTS=字符数，ASR=音频秒数（对应百炼计费维度）。
    cached_saves 仅对 TTS 有意义：命中缓存次数 = 省掉的上游合成次数。
    """
    _verify(request)
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT kind,
                   count(*) AS calls,
                   count(*) FILTER (WHERE status='error') AS errors,
                   count(*) FILTER (WHERE cached) AS cached_hits,
                   COALESCE(SUM(units) FILTER (WHERE status='success'), 0)::bigint AS units_total,
                   COALESCE(ROUND(AVG(latency_ms) FILTER (WHERE status='success')),0)::bigint AS avg_latency_ms,
                   max(created_at) AS last_at
            FROM speech_logs
            WHERE created_at >= NOW() - (? || ' hours')::interval
            GROUP BY kind
            """,
            (hours,),
        ).fetchall()
    by_kind = {r['kind']: dict(r) for r in rows}
    tts = by_kind.get('tts') or {}
    asr = by_kind.get('asr') or {}
    return {
        'hours': hours,
        'tts': {
            'calls': tts.get('calls', 0),
            'errors': tts.get('errors', 0),
            'cached_hits': tts.get('cached_hits', 0),
            'chars': tts.get('units_total', 0),
            'avg_latency_ms': tts.get('avg_latency_ms', 0),
            'last_at': tts.get('last_at'),
        },
        'asr': {
            'calls': asr.get('calls', 0),
            'errors': asr.get('errors', 0),
            'seconds': asr.get('units_total', 0),
            'avg_latency_ms': asr.get('avg_latency_ms', 0),
            'last_at': asr.get('last_at'),
        },
    }


@router.get('/stream')
async def stream(request: Request) -> StreamingResponse:
    """SSE 实时流：每 2s 轮询 call_logs 新行并推送，供面板实时刷新。

    鉴权用 query `?key=`（EventSource 不支持自定义 header）。
    """
    _verify(request)

    async def event_generator():
        last_id = 0
        try:
            with transaction() as conn:
                row = conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM call_logs").fetchone()
                last_id = int(row['m']) if row else 0
        except Exception:  # noqa: BLE001
            last_id = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                with transaction() as conn:
                    rows = conn.execute(
                        """
                        SELECT id, user_id, platform_id, session_id, model, status,
                               latency_ms, tool_calls, error, created_at
                        FROM call_logs
                        WHERE id > ?
                        ORDER BY id ASC
                        LIMIT 50
                        """,
                        (last_id,),
                    ).fetchall()
                for r in rows:
                    last_id = int(r['id'])
                    payload = json.dumps(dict(r), default=str, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.warning('[metrics] stream 轮询异常: %s', exc)
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )
