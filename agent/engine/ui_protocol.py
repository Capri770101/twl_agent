"""engine/ui_protocol.py —— 前端（小程序）渲染契约的 pydantic 模型。

所有 /chat 响应统一包成 ChatResponse；`ui` 字段决定前端如何渲染 `data`。
各 ui 类型对应的 data 子结构（DialogOption / PlanCard / ShopCard / OrderCard / PayJump）
既作为构造时的参考 schema，也供工具、技能、agent 直接复用，保证前后端字段一致。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class UIType(StrEnum):
    """UI 渲染类型。前端据此决定组件。"""

    TEXT = "text"
    DIALOG_OPTIONS = "dialog_options"
    PLAN_CARD = "plan_card"
    SHOP_CARD = "shop_card"
    ORDER_CARD = "order_card"
    PAY_JUMP = "pay_jump"
    IMAGE_TASK = "image_task"  # 生图结果：同步已 done 直接给 result_url，异步给 poll 轮询


class AgentActionType(StrEnum):
    """平台中立的下一步业务动作。"""

    NONE = "none"
    SHOW_TEXT = "show_text"
    SHOW_OPTIONS = "show_options"
    SHOW_PLAN = "show_plan"
    SHOW_SHOP = "show_shop"
    CREATE_ORDER = "create_order"
    OPEN_PAYMENT = "open_payment"
    START_IMAGE_TASK = "start_image_task"
    REQUEST_PLATFORM = "request_platform"


class AgentAction(BaseModel):
    """平台适配层消费的动作契约。"""

    type: AgentActionType = AgentActionType.NONE
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list)
    fallback: str = ""
class ToolCallRecord(BaseModel):
    """单次工具调用的对外记录。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    status: str = "ok"  # "ok" | "error"
class ChatResponse(BaseModel):
    """/chat 统一响应体（前端渲染契约）。"""

    user_id: str
    reply: str = ""
    ui: UIType = UIType.TEXT
    data: dict[str, Any] = Field(default_factory=dict)
    action: AgentAction = Field(default_factory=AgentAction)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    session_id: str = ""
    stage: str = ""  # 当前 SessionStage 值，便于前端感知进度


class ErrorResponse(BaseModel):
    """统一错误返回。"""

    code: int
    message: str


# --------------------------------------------------------------------------- #
# 各 ui 类型对应的 data 子结构（构造时复用，保证字段契约稳定）
# --------------------------------------------------------------------------- #


class DialogOption(BaseModel):
    label: str
    value: str


class PlanCard(BaseModel):
    plan_id: str
    name: str
    price: float
    desc: str = ""
    effect_image_url: str = ""
    merchant_name: str = ""


class ShopCard(BaseModel):
    shop_id: str
    name: str
    distance_km: float
    price_range: str = ""
    rating: float = 0.0


class OrderCard(BaseModel):
    order_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    total_price: float = 0.0
    plan_type: str = "existing"  # "existing" | "diy"


class PayJump(BaseModel):
    order_id: str
    page_path: str = "/pages/order/confirm"
    params: dict[str, Any] = Field(default_factory=dict)
