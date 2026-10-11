"""方案卡证据护栏：有实际花艺需求时防止只口述不存在的卡片。"""
from __future__ import annotations

import json
import re
from typing import Any

from agent.tools_meta import CARD_TOOLS as _CARD_TOOLS

_PLAN_QUESTION_WORDS = (
    '为什么', '为何', '怎么养', '如何养', '养护', '寓意', '花语', '有毒', '能吃',
    '区别', '什么是', '什么意思', '能放多久', '几天', '怎么保存', '真的吗',
)


def _card_produced(tool_log: list[Any]) -> bool:
    """本轮是否存在成功的方案/商品/店铺工具结果，而非模型口述。"""
    for tc in tool_log or []:
        if getattr(tc, 'status', '') != 'ok':
            continue
        name = getattr(tc, 'name', '')
        if name in _CARD_TOOLS:
            try:
                raw = getattr(tc, 'result', None)
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError, AttributeError):
                payload = None
            if isinstance(payload, dict) and (payload.get('ok') is False or payload.get('error') or payload.get('validation_status') == 'blocked'):
                continue
            return True
        if name == 'platform_db_query_entity':
            ent = str((getattr(tc, 'arguments', None) or {}).get('entity') or '')
            if ent in ('plan', 'shop'):
                try:
                    result = json.loads(tc.result) if isinstance(tc.result, str) else tc.result
                except (ValueError, TypeError):
                    result = None
                if isinstance(result, dict) and result.get('ok') is True and isinstance(result.get('data'), list) and result['data']:
                    return True
    return False


def _expresses_flower_need(message: str) -> bool:
    """用既有的纯正则需求抽取器识别本轮花艺需求。"""
    text = (message or '').strip()
    if not text:
        return False
    from agent.tools import extract_requirement
    req = extract_requirement(text)
    return bool(
        req.recipient or req.occasion or req.style or req.colors
        or req.budget_num or req.stem_count or req.single_flower
    )


_PROSE_QTY_PATTERNS = (
    re.compile(r'\d{1,3}\s*[朵支枝]'),
    re.compile(r'[\u4e00-\u9fff]{2,6}\s*[×xX]\s*\d{1,3}'),
)
_PROSE_PRICE_RE = re.compile(r'\d{2,4}\s*元')


def _looks_like_plan_prose(reply: str) -> bool:
    """首轮也不能凭空口述花材数量或「方案+价格」而不出卡。"""
    text = (reply or '').strip()
    if not text:
        return False
    if any(p.search(text) for p in _PROSE_QTY_PATTERNS):
        return True
    return '方案' in text and bool(_PROSE_PRICE_RE.search(text))


def _needs_card_nudge(message: str, tool_log: list[Any], has_context: bool, reply: str = '') -> bool:
    """仅有口述方案、没有可信卡片证据时要求模型重答。"""
    if _card_produced(tool_log):
        return False
    # 客服与订单金额不是花束设计，不能因此强制生成 DIY 卡片。
    if any(getattr(tc, 'name', '').startswith(('query_my_', 'contact_merchant',
                                               'cancel_my_order', 'apply_refund',
                                               'update_order_address')) for tc in tool_log):
        return False
    text = message or ''
    if any(w in text for w in ('我的订单', '查订单', '退款', '售后', '改地址', '取消订单')):
        return False
    if any(w in text for w in _PLAN_QUESTION_WORDS):
        return False
    from agent.engine.discussion import is_plan_discussion
    if is_plan_discussion(text):
        return False
    if _looks_like_plan_prose(reply):
        return True
    if not _expresses_flower_need(text):
        return False
    return has_context


def _card_nudge_text() -> str:
    """只进入本轮上下文的纠正指令。"""
    return (
        '[系统校验未通过] 你刚才**只用文字描述了方案，并没有真的生成方案卡**。\n'
        '本会话此前已经有过方案（或已追问过一轮），用户这句话表达的是花艺需求，'
        '必须给出可点击的方案卡，不能只用文字描述：\n'
        '· 要定制搭配 → 调用 generate_diy_plan；要在此基础上调整 → 调用 revise_diy_plan；\n'
        '· 要平台在售的现成款 → 调用 platform_db_query_entity(source_id=..., entity="plan")；\n'
        '· 拿到工具返回后，用 show_plan_card 输出卡片。\n'
        '⚠️ reply 里的方案名 / 花材组合 / 支数 / 价格，都必须来自工具返回，'
        '严禁凭记忆或想象编造（写不存在的商品名或错价是最严重的错误）。'
    )
