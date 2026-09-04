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
from agent.engine.ui_protocol import UIType
from agent.agent import ReActAgent
from backend.auth import current_user, require_user

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=['chat'])
logger = logging.getLogger('api')

_agent = ReActAgent()


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
async def chat(req: ChatRequest, authenticated_user: str | None = Depends(current_user)) -> Any:
    """与智能体对话，返回结构化 UI 响应。"""
    require_user(req.user_id, authenticated_user)
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
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail='处理超时，请简化问题后重试')
    except Exception as exc:
        logger.exception('智能体执行失败')
        raise HTTPException(status_code=500, detail='智能体执行失败，请稍后重试')

    final_sid = result.session_id
    await mem_store.update_conversation_preview(final_sid, req.message[:60])
    return result.model_dump()


@router.post('/chat/stream')
async def chat_stream(req: ChatRequest, authenticated_user: str | None = Depends(current_user)) -> StreamingResponse:
    """SSE 流式对话端点。"""
    require_user(req.user_id, authenticated_user)
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
            yield f"event: error\ndata: {json.dumps({'message': '处理过程中出现错误，请稍后重试'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type='text/event-stream',
                              headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})


@router.post('/chat/reset')
async def reset(req: ResetRequest, authenticated_user: str | None = Depends(current_user)) -> dict[str, Any]:
    """清空会话。"""
    require_user(req.user_id, authenticated_user)
    if req.session_id:
        await mem_store.delete_conversation(req.session_id, user_id=req.user_id)
        return {'ok': True, 'session_id': req.session_id}
    convs = await mem_store.list_conversations(req.user_id)
    if convs:
        await mem_store.delete_conversation(convs[0]['id'], user_id=req.user_id)
        return {'ok': True, 'session_id': convs[0]['id']}
    return {'ok': True}


@router.get('/conversations')
async def list_conversations(user_id: str, authenticated_user: str | None = Depends(current_user)) -> list[dict[str, Any]]:
    require_user(user_id, authenticated_user)
    return await mem_store.list_conversations(user_id)


@router.get('/conversations/{conversation_id}/messages')
async def get_messages(conversation_id: str, user_id: str, limit: int = 50, authenticated_user: str | None = Depends(current_user)) -> list[dict[str, Any]]:
    require_user(user_id, authenticated_user)
    conversation = await mem_store.get_conversation(conversation_id)
    if not conversation or conversation.get('user_id') != user_id:
        raise HTTPException(status_code=403, detail='无权访问该会话')
    limit = max(1, min(int(limit), 200))
    return await mem_store.load_history(conversation_id, limit)


@router.post('/conversations')
async def create_conversation(req: CreateConvRequest, authenticated_user: str | None = Depends(current_user)) -> dict[str, Any]:
    require_user(req.user_id, authenticated_user)
    cid = await mem_store.create_conversation(req.user_id, req.title or '新对话', shop_id=req.shop_id)
    return {'conversation_id': cid, 'id': cid}


@router.get('/tasks/{task_id}')
async def get_task(task_id: str, authenticated_user: str | None = Depends(current_user)) -> dict[str, Any]:
    """生图任务状态轮询（generate_effect_image 返回 poll 地址）。

    生产环境必须携带 Bearer 凭证，且任务按 user_id 归属校验；开发环境
    关闭鉴权时 task_id 为高熵随机值，仍可安全轮询。
    """
    if settings.AUTH_REQUIRED and not authenticated_user:
        raise HTTPException(status_code=401, detail='需要登录凭证')
    return await task_store.get_image_task(task_id, user_id=authenticated_user)


@router.get('/ui-contract')
async def ui_contract() -> dict[str, Any]:
    """返回全量 UI 类型清单（数据契约 + 渲染要求 + 示例）。

    供接入平台的前端 / AI 在对接时程序化对照自己实现了哪些组件，
    避免「智能体返回了结构化数据、前端却没组件渲染」的断层。
    与 FRONTEND_CONTRACT.md、agent/engine/ui_protocol.py 保持一致。
    """
    ui_types = [
        {
            'ui': UIType.TEXT.value,
            'action_type': 'show_text',
            'required_capabilities': [],
            'render': '文本气泡，渲染 reply 即可',
            'example': {},
        },
        {
            'ui': UIType.DIALOG_OPTIONS.value,
            'action_type': 'show_options',
            'required_capabilities': ['show_options'],
            'render': '选项按钮；点选后把 value 作为下一条消息回传 /chat',
            'example': {'options': [{'label': '现货花束（约200元）', 'value': 'existing'}, {'label': 'DIY 定制', 'value': 'diy'}]},
        },
        {
            'ui': UIType.PLAN_CARD.value,
            'action_type': 'show_plan',
            'required_capabilities': ['show_plan_page'],
            'render': '方案卡片列表：名称/价格/描述/效果图 + 确认、修改按钮',
            'example': {'plans': [{'plan_id': 'P001', 'name': '生日玫瑰花束', 'price': 199.0, 'desc': '红玫瑰+满天星', 'effect_image_url': '', 'merchant_name': '向阳花艺'}]},
        },
        {
            'ui': UIType.SHOP_CARD.value,
            'action_type': 'show_shop',
            'required_capabilities': ['show_shop_page'],
            'render': '店铺卡片列表：名称/距离/价格区间/评分 + 选择按钮',
            'example': {'shops': [{'shop_id': 'S001', 'name': '向阳花艺（五道口店）', 'distance_km': 1.2, 'price_range': '中高端', 'rating': 4.8}]},
        },
        {
            'ui': UIType.ORDER_CARD.value,
            'action_type': 'create_order',
            'required_capabilities': ['create_order'],
            'render': '订单确认卡：明细/合计/优惠 + 确认交互；订单写入由后端业务保证',
            'example': {'order_id': 'ORD_20260902_0001', 'items': [{'name': '红玫瑰', 'qty': 11, 'unit_price': 12.0, 'price': 132.0}], 'total_price': 199.0, 'plan_type': 'existing'},
        },
        {
            'ui': UIType.PAY_JUMP.value,
            'action_type': 'open_payment',
            'required_capabilities': ['open_payment'],
            'render': '提供“去支付”入口，用 data 打开平台自己的收银/支付页（智能体不接触支付密钥）',
            'example': {'order_id': 'ORD_20260902_0001', 'page_path': '/pages/order/confirm', 'params': {'order_id': 'ORD_20260902_0001', 'total_price': 199.0}},
        },
        {
            'ui': UIType.IMAGE_TASK.value,
            'action_type': 'start_image_task',
            'required_capabilities': ['start_image_task'],
            'render': '生图进度：展示生成中 + 按 poll 轮询 GET /tasks/{task_id}；成功后展示 result_url/image_url',
            'example': {'task_id': 'task_img_0001', 'poll': '/tasks/task_img_0001', 'result_url': ''},
        },
        {
            'ui': UIType.GREETING_CARD.value,
            'action_type': 'show_greeting_card',
            'required_capabilities': ['show_greeting_card'],
            'render': '电子贺卡：直接展示 data.image_url 大图 + 文案 + 「换模板/改文案重做」入口（同步完成，无需轮询）',
            'example': {'image_url': '/generated/greet_20260904120000_ab12cd.png', 'text': '妈妈，生日快乐！……', 'recipient': '亲爱的妈妈', 'sender': '爱你的女儿', 'template': 'warm', 'note': '模板 warm（温暖奶油风：米金渐变、暖色小花，适合家人/温馨场合）'},
        },
    ]
    return {
        'ui_types': ui_types,
        'required_components': ['text', 'dialog_options', 'plan_card', 'shop_card', 'order_card', 'pay_jump', 'image_task', 'greeting_card'],
        'contract_doc': 'FRONTEND_CONTRACT.md',
        'schema_source': 'agent/engine/ui_protocol.py',
        'note': '本后端只产出结构化 ui/data/action，前端渲染由宿主平台负责。接入前请先实现上述组件，否则会出现“有数据无展示”。',
    }
