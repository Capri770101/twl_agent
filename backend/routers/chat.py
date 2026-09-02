"""简化版 chat.py —— 智能体对话端点，用于独立部署。

鉴权说明：本服务为纯智能体封装，不含用户管理。
宿主平台（小程序/H5）负责用户登录，调用时直接传 user_id 即可。
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any

from backend.config import settings
from backend.storage import memory as mem_store
from backend.storage import tasks as task_store
from agent.agent import ReActAgent

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=['chat'])
logger = logging.getLogger('api')

_agent = ReActAgent(session_store=mem_store, task_store=task_store)


def get_agent() -> ReActAgent:
    return _agent


class ChatRequest(BaseModel):
    message: str
    user_id: str  # 必填，由宿主平台传入
    session_id: str | None = None
    location: dict[str, Any] | None = None
    shop_id: str | None = None


class ResetRequest(BaseModel):
    user_id: str
    session_id: str | None = None


class CreateConvRequest(BaseModel):
    user_id: str
    title: str | None = None
    shop_id: str | None = None


@router.post('/chat')
async def chat(req: ChatRequest) -> Any:
    """与智能体对话，返回结构化 UI 响应。"""
    logger.info('chat user=%s msg=%s', req.user_id, req.message[:80])

    sid = req.session_id
    if sid:
        conv = await mem_store.get_conversation(sid)
        if not conv or conv.get('user_id') != req.user_id:
            sid = None
    if not sid:
        sid = await mem_store.create_conversation(req.user_id, title=req.message[:20], shop_id=req.shop_id)

    try:
        result = await asyncio.wait_for(
            get_agent().arun(req.user_id, req.message, sid, req.location, shop_id=req.shop_id),
            timeout=settings.REQUEST_TIMEOUT
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail='处理超时，请简化问题后重试')
    except Exception as exc:
        logger.exception('智能体执行失败')
        raise HTTPException(status_code=500, detail=f'智能体执行失败: {type(exc).__name__}')

    final_sid = result.session_id
    await mem_store.update_conversation_preview(final_sid, req.message[:60])
    return result.model_dump()


@router.post('/chat/stream')
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式对话端点。"""
    logger.info('chat/stream user=%s msg=%s', req.user_id, req.message[:80])

    sid = req.session_id
    if sid:
        conv = await mem_store.get_conversation(sid)
        if not conv or conv.get('user_id') != req.user_id:
            sid = None
    if not sid:
        sid = await mem_store.create_conversation(req.user_id, title=req.message[:20], shop_id=req.shop_id)

    async def event_generator():
        try:
            async for evt in get_agent().arun_stream(req.user_id, req.message, sid, req.location, shop_id=req.shop_id):
                event_type = evt.get('event', 'text')
                data = {k: v for k, v in evt.items() if k != 'event'}
                yield f'event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
        except Exception as exc:
            logger.exception('SSE 流异常')
            yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})


@router.post('/chat/reset')
async def reset(req: ResetRequest) -> dict[str, Any]:
    """清空会话。"""
    if req.session_id:
        await mem_store.delete_conversation(req.session_id)
        return {'ok': True, 'session_id': req.session_id}
    convs = await mem_store.list_conversations(req.user_id)
    if convs:
        await mem_store.delete_conversation(convs[0]['id'])
        return {'ok': True, 'session_id': convs[0]['id']}
    return {'ok': True}


@router.get('/conversations')
async def list_conversations(user_id: str) -> list[dict[str, Any]]:
    return await mem_store.list_conversations(user_id)


@router.get('/conversations/{conversation_id}/messages')
async def get_messages(conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return await mem_store.load_history(conversation_id, limit)


@router.post('/conversations')
async def create_conversation(req: CreateConvRequest) -> dict[str, Any]:
    cid = await mem_store.create_conversation(req.user_id, req.title or '新对话', shop_id=req.shop_id)
    return {'conversation_id': cid, 'id': cid}


@router.get('/tasks/{task_id}')
async def get_task(task_id: str) -> dict[str, Any]:
    """生图任务状态轮询（generate_effect_image 返回 poll 地址）。"""
    return await task_store.get_image_task(task_id)
