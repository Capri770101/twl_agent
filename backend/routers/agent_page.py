"""官网演示页适配端点：``POST /api/agent/query``。

用途：``www.tiaowulan.com/agent.html`` 的体验窗切到 live 后调用本端点。
设计取舍（与页面客户端契约对齐，改前先读 `agent/client_payload.py` 的模块文档）：

- **无状态单轮**：页面没有对话历史、输入框每次只发一句话，所以这里不做多轮；
  用户想补充信息就在输入框再说一句（页面语义本来就是"一次生成一个方案"）。
- **纯规则引擎**（不调 LLM）：公开页面的流量不可预期，规则引擎 <100ms、零成本、
  结果稳定，不会因为模型波动或超时让页面降级；想要更"聪明"的表达可在
  `agent/client_payload.py::design_plan` 处换成 LLM 路径（需另配限流与预算）。
- **自动降级**：页面客户端对非 2xx 会 catch 并回退到自己的 demo 引擎，
  所以这里出错时直接返回 4xx/5xx 即可，不会白屏。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent.client_payload import build_client_payload
from backend.config import settings
from backend.rate_limit import check_rate_limit

logger = logging.getLogger('backend.agent_page')

router = APIRouter(tags=['agent-page'])


class PageQuery(BaseModel):
    """页面请求体：原始输入 + 页面自己解析出的四要素。"""

    input: str = Field(default='', description='用户原始输入')
    parsed: dict[str, Any] = Field(default_factory=dict, description='页面解析的四要素 recipient/scene/style/budget')


@router.post('/api/agent/query')
async def agent_query(req: PageQuery, request: Request) -> dict[str, Any]:
    """体验窗适配入口（单轮、无状态、按 IP 限流）。

    Args:
        req: ``{input, parsed}``。
        request: 用于取客户端 IP 做限流维度。

    Returns:
        页面契约响应（结构见 ``agent/client_payload.py`` 模块文档）。

    Raises:
        HTTPException: 404 未启用；422 输入为空；429 触发限流；500 生成失败；504 生成超时。
    """
    if not settings.AGENT_PAGE_ENABLED:
        # 默认关闭时对外表现为"端点不存在"，避免未准备好时被扫到
        raise HTTPException(status_code=404, detail='not found')

    ip = request.client.host if request.client else 'unknown'
    allowed, retry_after = check_rate_limit(f'agentpage:{ip}')
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail='请求过于频繁，请稍后再试',
            headers={'Retry-After': str(retry_after)},
        )

    text = (req.input or '').strip()
    if not text:
        raise HTTPException(status_code=422, detail='input 不能为空')
    if len(text) > settings.AGENT_PAGE_MAX_INPUT:
        text = text[: settings.AGENT_PAGE_MAX_INPUT]

    try:
        # 规则引擎是纯 CPU 计算，放线程池避免阻塞事件循环；再加总超时兜底
        return await asyncio.wait_for(
            asyncio.to_thread(build_client_payload, text, req.parsed or {}),
            timeout=settings.AGENT_PAGE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning('[agent_page] 生成超时 ip=%s', ip)
        raise HTTPException(status_code=504, detail='生成超时，请重试')
    except HTTPException:
        raise
    except Exception:
        logger.exception('[agent_page] 生成失败 ip=%s', ip)
        raise HTTPException(status_code=500, detail='生成失败，请重试')
