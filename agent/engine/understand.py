"""understand.py —— v2 编排脊柱的理解层：模型先声明意图与需求，路由再查表。

设计原则（与 v1 关键词层的根本区别）：
- **意图由模型理解后声明**，不再用关键词正则猜（关键词实测会误伤：
  「你agent自己的原因」里的「自己」曾误命中悦己场景）。
- **输出必须过白名单校验**：intent 只认路由表里的策略名；需求槽位走
  ``merge_requirement`` 的既有白名单（花名表/场景表/值域），模型幻觉进不来。
- **理解失败降级为全量工具**（等价 v1 的 route=None），不阻塞主流程。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent.engine.llm import call_llm
from agent.orchestration.policies.routing import load_routing_policies

logger = logging.getLogger('agent')

_POLICIES = load_routing_policies()

_INTENT_DESC = {
    'design': '定制或修改花束方案（含「改成/换个配色」这类改版）',
    'buying': '买/推荐现成在售花束',
    'qa': '花卉知识、养护、花语咨询，或讨论既有方案细节（不授权改动）',
    'greeting': '写祝福语/做贺卡',
    'image': '要或不要效果图',
    'reminder': '设置/管理提醒',
    'customer_service_orders': '查本人订单、退款、售后、取消、改地址',
    'customer_service_tickets': '查本人登记的工单/商家联系记录',
    'customer_service_contact': '要联系商家、催单、投诉、转人工',
    'shop_service_info': '问店铺营业时间/地址/配送',
    'shop_service_reviews': '问店铺评价口碑',
    'chitchat': '纯闲聊寒暄（你好/谢谢/再见）',
    'meta': '在问系统或助手本身：你是谁、为什么没查到、出了什么问题、是不是你的原因',
    'other': '以上都不是',
}

_PROMPT = (
    '你是花艺智能体的需求理解器。读用户消息，只输出一个 JSON 对象，不要任何解释或 markdown。\n'
    '字段：\n'
    '"intent"：本轮真实意图，取值与含义如下（判断依据是用户想干什么，不是出现了哪个词）：\n'
    + '\n'.join(f'  {k} = {v}' for k, v in _INTENT_DESC.items())
    + '\n"requirement"：送花需求槽位，仅写**用户原话明确表达**的，没读到的一律不填或 null：\n'
    '  recipient/occasion/style/mood/colors/budget/stem_count/single_flower/gift_intent/gift_message\n'
    '  gift_intent 取值：gratitude/apology/affection/comfort/encouragement/celebration/remembrance；'
    '它是用户想让收花人感受到的心意，不是用户自己的情绪，也不是花束风格。\n'
    '"missing"：若用户要直接出方案，还缺哪些关键信息（recipient/occasion/budget/style/colors），'
    '不缺填 []；用户说随便/你决定时视为已授权，填 []。\n'
    '"needs_platform"：本轮在问哪类平台事实——问在售商品/花束（价格/库存/花材/色系，'
    '含「现成的/想买/推荐一束」）填 "plan"；问店铺本体（营业时间/地址/配送/评价口碑）才填 "shop"；'
    '两者都不问填 ""。\n'
    '"plan_discussion"：true/false（是否在讨论既有方案而非授权新设计）。\n'
    '严格按原话判断，绝不根据常识推测。\n\n用户消息：'
)

_JSON_RE = re.compile(r'\{.*\}', re.S)


@dataclass(frozen=True)
class Understanding:
    """一轮用户消息的结构化理解（模型声明 + 白名单校验后）。"""

    intent: str | None = None
    requirement: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    needs_platform: str = ''
    plan_discussion: bool = False
    raw_ok: bool = True


def _validate(payload: Any) -> Understanding | None:
    """模型输出过白名单；整体非法返回 None（调用方降级为全量工具）。"""
    if not isinstance(payload, dict):
        return None
    intent = payload.get('intent')
    if not isinstance(intent, str) or intent not in _POLICIES and intent not in _INTENT_DESC:
        intent = None
    req = payload.get('requirement')
    missing = payload.get('missing')
    needs_platform = str(payload.get('needs_platform') or '')
    if needs_platform not in ('', 'shop', 'plan'):
        needs_platform = ''
    return Understanding(
        intent=intent,
        requirement=req if isinstance(req, dict) else {},
        missing=[str(m) for m in missing] if isinstance(missing, list) else [],
        needs_platform=needs_platform,
        plan_discussion=bool(payload.get('plan_discussion')),
    )


def understand(message: str, *, timeout: float | None = None, user_id: str | None = None) -> Understanding | None:
    """让模型声明本轮意图与需求槽位；失败返回 None（降级为不缩圈）。

    timeout 未显式传入时用 ``settings.UNDERSTAND_TIMEOUT``（默认 10s）：
    理解层输出只是一个几百 token 的 JSON，不该按整轮 LLM 预算（120s）等待——
    慢 provider 挂住理解层时，快速降级为全量工具比拖着整轮更符合用户利益。

    故意不发工具、不用会话历史：理解器只判断「这句话本身要什么」，
    跨轮上下文由 requirement 累积层负责（与 v1 口径一致）。
    """
    from backend.config import settings
    if not settings.AGENT_UNDERSTAND_ENABLED:
        return None
    if not settings.llm_api_key:
        return None
    if timeout is None:
        timeout = settings.UNDERSTAND_TIMEOUT
    try:
        resp = call_llm(
            [{'role': 'user', 'content': _PROMPT + message}],
            tools=None, timeout=min(timeout, settings.llm_timeout), user_id=user_id,
        )
        content = getattr(resp.choices[0].message, 'content', '') or ''
        m = _JSON_RE.search(content)
        if not m:
            return None
        return _validate(json.loads(m.group()))
    except Exception:
        logger.warning('[understand] 理解层调用失败，降级为不缩圈', exc_info=True)
        return None
