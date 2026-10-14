"""交易声称护栏：没有真实交易工具证据时，不得声称下单/支付已完成。

与 image_claim（谎称已出图）同构：检测对象是**模型的回复**（产出物），
证据来源是**本轮工具日志**——create_order 受配置门禁（skills/skill_order.py +
toolkit.order_write_enabled，默认关闭）：
- 通道未接通（默认）：任何「已下单/支付成功」的声称都不可能有工具支撑，必假；
- 通道已接通：声称完成的**那一个**必须有本轮 create_order 成功记录背书，
  没调工具就说已下单仍然是谎称（护栏判据不变，始终只看证据）。
这与关键词意图分类无关：用户说什么是模型去理解的，我们只校验模型自己说了什么。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger('agent')

# 只拦「完成态」声称；「去下单」「可以下单」「请在小程序下单」这类引导不算。
# 前缀或后缀必须有一个成立——否则「下单」一词裸匹配会误伤正常购买引导
# （2026-10-13 smoke 场景 4 实测误伤：订单列表回复被整段替换）。
_TRADE_CLAIM_RE = re.compile(
    r'已(?:经|成功|为你|帮你)(?:下单|支付|付款)'
    r'|(?:下单|支付|付款)(?:成功|完成|好了)'
    r'|订单已(?:生成|提交|创建)'
)
_TRADE_NEG_WORDS = ('不能下单', '无法下单', '不支持下单', '未下单', '没有下单', '尚未下单', '不会下单')

_TRADE_UNVERIFIED_REPLY = (
    '抱歉，我刚才的说法不准确——这个系统不能直接为你下单或收款，'
    '也没有任何订单真的被创建。购买请在平台/小程序的商品页完成；'
    '需要的话我可以帮你查在售商品或定制方案。'
)

_TRADE_UNVERIFIED_REPLY_CHANNEL_OPEN = (
    '抱歉，我刚才的说法不准确——本轮并没有真正提交订单，没有任何订单被创建。'
    '如果确认要买，我可以通过下单通道为你提交；也可以到平台/小程序的商品页自行完成。'
)


def _order_channel_open() -> bool:
    """平台下单写通道是否已接通（决定纠正文案口径，不影响证据判据）。"""
    try:
        from agent.toolkit import order_write_enabled
        return order_write_enabled()
    except Exception:
        return False


def _trade_tool_succeeded(tool_log: list[Any]) -> bool:
    """本轮是否存在成功的交易工具结果（当前无此工具，恒为 False）。"""
    for tc in tool_log or []:
        if getattr(tc, 'name', '') != 'create_order' or getattr(tc, 'status', '') != 'ok':
            continue
        try:
            result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result or {}
        except (json.JSONDecodeError, TypeError):
            result = {}
        if isinstance(result, dict) and not result.get('error'):
            return True
    return False


def _claims_trade_done(reply: str) -> bool:
    """回复是否声称交易已完成（否定表述不算）。"""
    text = reply or ''
    if not text:
        return False
    for clause in re.split(r'[。！？!?；;，,\n]+', text):
        if _TRADE_CLAIM_RE.search(clause) and not any(w in clause for w in _TRADE_NEG_WORDS):
            return True
    return False


def _needs_trade_nudge(tool_log: list[Any], reply: str) -> bool:
    """无交易工具证据却声称交易完成时，要求模型重答。"""
    return not _trade_tool_succeeded(tool_log) and _claims_trade_done(reply)


def _trade_nudge_text() -> str:
    """本轮纠正指令（不落库）；口径按下单通道是否接通区分。"""
    if _order_channel_open():
        return (
            '[系统校验未通过] 你刚才声称「已下单 / 支付成功」，但**本轮没有 create_order 成功记录**'
            '——用户会拿着这句假话去找不存在的订单，这是最严重的错误。\n'
            '请如实告知尚未提交订单；用户确认要买时再调用 create_order，拿到 order_id 后才能说已下单。'
        )
    return (
        '[系统校验未通过] 你刚才声称「已下单 / 支付成功」，但**本轮没有任何真实交易工具被调用**'
        '（本系统也不支持代客下单）——用户会拿着这句假话去找不存在的订单，这是最严重的错误。\n'
        '请如实告知：本服务不能直接下单/收款，购买须在平台/小程序完成；'
        '如果用户想要买花，帮他查在售商品或做 DIY 方案即可。'
    )


def _finalize_trade_claim(reply: str, tool_log: list[Any]) -> str:
    """模型重答后仍谎称交易完成，替换为确定性如实说明。"""
    if _trade_tool_succeeded(tool_log) or not _claims_trade_done(reply):
        return reply
    logger.warning('[agent] 回复谎称交易完成但本轮无交易工具证据，已替换为如实说明')
    return _TRADE_UNVERIFIED_REPLY_CHANNEL_OPEN if _order_channel_open() else _TRADE_UNVERIFIED_REPLY
